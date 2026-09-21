-- Coloque este arquivo como Script server-side no Roblox Studio.
-- O Roblox Bridge deve decodificar JSON e chamar runtime:handle(command).
-- Este módulo não interpreta TikTok nem executa código vindo do payload.

local LiveRuntime = require(script.Parent.LiveRuntime)

local runtime = LiveRuntime.new({
    containerName = "LiveActors",
    maxActiveAvatars = 100,
    commentLifetimeSeconds = 60,
    giftLifetimeSeconds = 300,
})

runtime:start()

-- Integração esperada com o Bridge, na fase de transporte:
-- local ok, reason = runtime:handle(decodedCommand)
-- if not ok then warn("LiveRuntime rejected command", reason) end

return runtime
