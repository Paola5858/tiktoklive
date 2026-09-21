--[[
	BridgeClient.lua — consumidor HTTP da Local API, do lado Roblox.

	Onde colocar: um Script (server-side) dentro de ServerScriptService.
	Isto roda no SERVIDOR do jogo, não em um LocalScript nem em Studio Plugin.

	ATENÇÃO — leia antes de configurar BASE_URL (ver context/ROBLOX_BRIDGE.md
	pra a explicação completa e as fontes oficiais consultadas):

	- Em Studio (Play Solo / Team Test), o servidor da experiência roda na
	  SUA própria máquina, então `http://localhost:PORTA` ou
	  `http://127.0.0.1:PORTA` podem funcionar — a documentação atual da
	  Roblox mostra um exemplo oficial conectando a um servidor local
	  (Ollama, via HttpService:CreateWebStreamClient) exatamente assim.
	- Numa experiência PUBLICADA, o servidor roda na nuvem da Roblox, uma
	  máquina completamente diferente da sua. `localhost` ali NUNCA vai
	  apontar pro seu computador. Pra isso funcionar fora do Studio, a
	  Local API precisa estar exposta num endereço público (ex: um túnel
	  HTTPS como ngrok/Cloudflare Tunnel) — troque BASE_URL por essa URL.
	- Isto ainda não foi testado manualmente dentro do Studio de verdade
	  (ver TEST_PLAN.md, "manual_studio_test" — pendente). O comportamento
	  acima está documentado a partir da documentação oficial da Roblox,
	  não de um teste real feito por este agente.

	Responsabilidades deste módulo (ver ROBLOX_BRIDGE.md):
	- fazer polling em GET /events
	- validar schema_version e formato da resposta
	- ignorar duplicatas (idempotência do lado Roblox)
	- avançar o cursor e opcionalmente confirmar via POST /ack
	- backoff exponencial controlado em falha, reset após sucesso
	- encaminhar cada evento pro Game Event Router (function table abaixo)

	O que este módulo explicitamente NÃO faz (fase 4, avatar_scope):
	- não faz spawn de avatar, não decide efeito de gift
	- a composição da fase 6 registra o handler GAME_COMMAND em init.server.lua
--]]

local HttpService = game:GetService("HttpService")

-- ===========================================================================
-- Configuração
-- ===========================================================================

local CONFIG = {
	-- NUNCA deixe isto como "localhost" ao publicar a experiência — ver o
	-- comentário no topo do arquivo. Ajuste pra URL de túnel quando for
	-- além de teste em Studio.
	BASE_URL = "http://localhost:8787",

	POLL_INTERVAL_SECONDS = 2, -- baseline — não é número mágico, calibrar com métricas reais
	MAX_BACKOFF_SECONDS = 30,
	BACKOFF_SEQUENCE = { 2, 4, 8, 16, 30 }, -- espelha o backoff do lado Python (DECISIONS.md)
	EVENTS_PER_POLL = 25,
	DEDUP_CACHE_CAPACITY = 500, -- bounded — nunca guarda IDs infinitamente
	SCHEMA_VERSION = "1.0",
}

-- ===========================================================================
-- Estado interno
-- ===========================================================================

local State = {
	running = false,
	cursor = 0,
	backoffIndex = 0,
	consecutiveFailures = 0,
	lastSuccessfulPollAt = nil,
	lastEventReceivedAt = nil,
	-- Dedup cache bounded: tabela de presença + fila de ordem de inserção,
	-- pra saber o que descartar quando a capacidade estoura.
	dedupSeen = {},
	dedupOrder = {},
}

-- ===========================================================================
-- Game Event Router — transporte; gameplay fica no LiveRuntime
-- ===========================================================================

-- Handlers registráveis por event_type. O handler GAME_COMMAND é registrado
-- pela composição server-side em init.server.lua.
local GameEventRouter = {}
GameEventRouter._handlers = {}

function GameEventRouter.register(eventType: string, handler: (table) -> ())
	GameEventRouter._handlers[eventType] = handler
end

function GameEventRouter.route(envelope: table)
	local handler = GameEventRouter._handlers[envelope.event_type]
	if handler == nil then
		-- Evento desconhecido/sem handler não derruba o consumidor.
		print(("[BridgeClient] evento sem handler: %s (id=%s)"):format(
			tostring(envelope.event_type),
			tostring(envelope.event_id)
		))
		return
	end

	local ok, err = pcall(handler, envelope)
	if not ok then
		warn(("[BridgeClient] handler falhou pra %s: %s"):format(envelope.event_type, tostring(err)))
	end
end

-- Handlers de observabilidade para eventos legados.
GameEventRouter.register("COMMENT", function(envelope)
	print(("[BridgeClient] COMMENT de %s: %s"):format(
		envelope.user and envelope.user.display_name or "?",
		envelope.payload and envelope.payload.text or ""
	))
end)

GameEventRouter.register("AGGREGATED", function(envelope)
	print(("[BridgeClient] AGGREGATED %s x%d"):format(
		tostring(envelope.payload and envelope.payload.original_event_type),
		envelope.payload and envelope.payload.count or 0
	))
end)

-- A composição server-side registra o handler real em init.server.lua.
-- O transporte apenas encaminha o payload; validação e allowlist ficam no
-- LiveRuntime/GameEventRouter.

-- ===========================================================================
-- Deduplicação (idempotência do lado Roblox — ver ROBLOX_BRIDGE.md)
-- ===========================================================================

local function isDuplicate(eventId: string): boolean
	return State.dedupSeen[eventId] == true
end

local function markSeen(eventId: string)
	if State.dedupSeen[eventId] then
		return
	end
	State.dedupSeen[eventId] = true
	table.insert(State.dedupOrder, eventId)

	if #State.dedupOrder > CONFIG.DEDUP_CACHE_CAPACITY then
		local oldest = table.remove(State.dedupOrder, 1)
		State.dedupSeen[oldest] = nil
	end
end

-- ===========================================================================
-- Transporte HTTP
-- ===========================================================================

local function requestEvents(): (boolean, table?)
	local url = ("%s/events?since=%d&limit=%d"):format(
		CONFIG.BASE_URL,
		State.cursor,
		CONFIG.EVENTS_PER_POLL
	)

	local ok, response = pcall(function()
		return HttpService:RequestAsync({
			Url = url,
			Method = "GET",
		})
	end)

	if not ok then
		-- Falha de transporte (DNS, conexão recusada, etc.) — não derruba o jogo.
		warn("[BridgeClient] request falhou:", tostring(response))
		return false, nil
	end

	if not response.Success then
		warn(("[BridgeClient] HTTP %s: %s"):format(
			tostring(response.StatusCode),
			tostring(response.StatusMessage)
		))
		return false, nil
	end

	local decodeOk, body = pcall(function()
		return HttpService:JSONDecode(response.Body)
	end)

	if not decodeOk then
		warn("[BridgeClient] JSON inválido na resposta de /events:", tostring(body))
		return false, nil
	end

	if type(body) ~= "table" or type(body.events) ~= "table" then
		warn("[BridgeClient] formato de resposta inesperado em /events")
		return false, nil
	end

	if body.schema_version ~= CONFIG.SCHEMA_VERSION then
		-- Rejeição segura de versão desconhecida — não tenta interpretar
		-- um contrato que não reconhece (ver schema_versioning na spec).
		warn(("[BridgeClient] schema_version desconhecida: %s (esperado %s)"):format(
			tostring(body.schema_version),
			CONFIG.SCHEMA_VERSION
		))
		return false, nil
	end

	if body.gap_detected then
		warn("[BridgeClient] gap detectado: eventos foram descartados antes de serem consumidos")
	end

	return true, body
end

local function sendAck(upToSequence: number)
	-- Best-effort: falha de ack não é crítica nesta fase (observabilidade,
	-- não controle de fluxo — ver RobloxBridge.ack no lado Python).
	pcall(function()
		HttpService:RequestAsync({
			Url = CONFIG.BASE_URL .. "/ack",
			Method = "POST",
			Headers = { ["Content-Type"] = "application/json" },
			Body = HttpService:JSONEncode({ up_to_sequence = upToSequence }),
		})
	end)
end

-- ===========================================================================
-- Loop de polling com backoff
-- ===========================================================================

local function currentBackoffSeconds(): number
	if State.backoffIndex <= 0 then
		return 0
	end
	local seq = CONFIG.BACKOFF_SEQUENCE
	local idx = math.min(State.backoffIndex, #seq)
	return seq[idx]
end

local function pollOnce()
	local ok, body = requestEvents()

	if not ok then
		State.consecutiveFailures += 1
		State.backoffIndex = math.min(State.backoffIndex + 1, #CONFIG.BACKOFF_SEQUENCE)
		return
	end

	-- Sucesso: reseta backoff.
	State.consecutiveFailures = 0
	State.backoffIndex = 0
	State.lastSuccessfulPollAt = os.time()

	local highestSeen = State.cursor
	for _, envelope in ipairs(body.events) do
		if not isDuplicate(envelope.event_id) then
			markSeen(envelope.event_id)
			State.lastEventReceivedAt = os.time()
			GameEventRouter.route(envelope)
		end
		if envelope.sequence_number and envelope.sequence_number > highestSeen then
			highestSeen = envelope.sequence_number
		end
	end

	-- Cursor avança pro maior sequence_number visto nesta página, ou pro
	-- cursor do servidor se não veio nenhum evento novo (evita ficar
	-- pedindo `since` desatualizado indefinidamente).
	State.cursor = math.max(highestSeen, body.cursor or State.cursor)
	sendAck(State.cursor)
end

local function pollLoop()
	while State.running do
		pollOnce()

		local backoff = currentBackoffSeconds()
		local waitTime = backoff > 0 and backoff or CONFIG.POLL_INTERVAL_SECONDS
		task.wait(waitTime)
	end
end

-- ===========================================================================
-- Lifecycle público
-- ===========================================================================

local BridgeClient = {}

function BridgeClient.start()
	if State.running then
		return -- idempotente
	end
	if not HttpService.HttpEnabled then
		warn(
			"[BridgeClient] HttpService.HttpEnabled está false. " ..
			"Habilite em File > Experience Settings > Security > Allow HTTP Requests."
		)
	end
	State.running = true
	task.spawn(pollLoop)
end

function BridgeClient.stop()
	State.running = false -- idempotente: chamar de novo não faz nada
end

function BridgeClient.getState()
	return {
		running = State.running,
		cursor = State.cursor,
		consecutiveFailures = State.consecutiveFailures,
		lastSuccessfulPollAt = State.lastSuccessfulPollAt,
		lastEventReceivedAt = State.lastEventReceivedAt,
	}
end

BridgeClient.GameEventRouter = GameEventRouter

game:BindToClose(function()
	BridgeClient.stop()
end)

return BridgeClient
