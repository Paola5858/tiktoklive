-- Coloque este arquivo como Script server-side no Roblox Studio.
-- O Roblox Bridge deve decodificar JSON e chamar runtime:handle(command).
-- Este módulo não interpreta TikTok nem executa código vindo do payload.

local LiveRuntime = require(script.Parent.LiveRuntime)
local BridgeClient = require(script.Parent.BridgeClient)

local runtime = LiveRuntime.new({
    containerName = "LiveActors",
    maxActiveAvatars = 100,
    commentLifetimeSeconds = 60,
    giftLifetimeSeconds = 300,
})

runtime:start()

BridgeClient.GameEventRouter.register("GAME_COMMAND", function(envelope)
    local ok, reason = runtime:handle(envelope.payload)
    if not ok then
        warn("[LiveRuntime] comando rejeitado", reason)
    end
end)
BridgeClient.start()

-- Garante cleanup das instâncias temporárias quando o servidor fechar.
-- stop() é idempotente: pode ser chamado mais de uma vez com segurança.
game:BindToClose(function()
    BridgeClient.stop()
    runtime:stop()
end)

-- Integração esperada com o Bridge, na fase de transporte:
-- local ok, reason = runtime:handle(decodedCommand)
-- if not ok then warn("LiveRuntime rejected command", reason) end

return runtime
