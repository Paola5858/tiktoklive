local RuntimeConfig = {}

RuntimeConfig.DEFAULTS = {
    containerName = "LiveActors",
    maxActiveAvatars = 100,
    commentLifetimeSeconds = 60,
    giftLifetimeSeconds = 300,
    specialLifetimeSeconds = 120,
    avatarCacheMaxEntries = 256,
    avatarCacheTtlSeconds = 900,
    idempotencyCacheMaxEntries = 2048,
    idempotencyTtlSeconds = 900,
    maxPendingSpawns = 100,
    maxSpawnsPerSecond = 10,
    spawnOrigin = Vector3.new(0, 3, 0),
    spawnSpacing = Vector3.new(4, 0, 0),
    fallbackColor = Color3.fromRGB(100, 170, 255),
}

local function positiveNumber(value, name)
    assert(type(value) == "number" and value > 0, name .. " must be a positive number")
    return value
end

function RuntimeConfig.new(overrides)
    overrides = overrides or {}
    local config = {}
    for key, value in pairs(RuntimeConfig.DEFAULTS) do
        config[key] = overrides[key] ~= nil and overrides[key] or value
    end

    assert(type(config.containerName) == "string" and #config.containerName > 0, "containerName must be non-empty")
    positiveNumber(config.maxActiveAvatars, "maxActiveAvatars")
    positiveNumber(config.commentLifetimeSeconds, "commentLifetimeSeconds")
    positiveNumber(config.giftLifetimeSeconds, "giftLifetimeSeconds")
    positiveNumber(config.specialLifetimeSeconds, "specialLifetimeSeconds")
    positiveNumber(config.avatarCacheMaxEntries, "avatarCacheMaxEntries")
    positiveNumber(config.avatarCacheTtlSeconds, "avatarCacheTtlSeconds")
    positiveNumber(config.idempotencyCacheMaxEntries, "idempotencyCacheMaxEntries")
    positiveNumber(config.idempotencyTtlSeconds, "idempotencyTtlSeconds")
    positiveNumber(config.maxPendingSpawns, "maxPendingSpawns")
    positiveNumber(config.maxSpawnsPerSecond, "maxSpawnsPerSecond")
    assert(typeof(config.spawnOrigin) == "Vector3", "spawnOrigin must be Vector3")
    assert(typeof(config.spawnSpacing) == "Vector3", "spawnSpacing must be Vector3")
    assert(typeof(config.fallbackColor) == "Color3", "fallbackColor must be Color3")
    return config
end

return RuntimeConfig
