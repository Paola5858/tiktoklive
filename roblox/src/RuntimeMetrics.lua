local RuntimeMetrics = {}
RuntimeMetrics.__index = RuntimeMetrics

function RuntimeMetrics.new()
    return setmetatable({
        activeAvatars = 0,
        peakActiveAvatars = 0,
        spawnRequests = 0,
        spawnSuccess = 0,
        spawnFailure = 0,
        droppedSpawnRequests = 0,
        fallbackAvatarUsage = 0,
        avatarCacheHits = 0,
        avatarCacheMisses = 0,
        avatarLookupFailures = 0,
        cleanupCount = 0,
        cleanupFailures = 0,
        activeEffects = 0,
        duplicateCommands = 0,
        rejectedCommands = 0,
        resourceLimitHits = 0,
    }, RuntimeMetrics)
end

function RuntimeMetrics:setActiveAvatars(value)
    self.activeAvatars = value
    if value > self.peakActiveAvatars then
        self.peakActiveAvatars = value
    end
end

function RuntimeMetrics:record(name, amount)
    local current = self[name]
    if type(current) == "number" then
        self[name] = current + (amount or 1)
    end
end

function RuntimeMetrics:snapshot()
    local copy = {}
    for key, value in pairs(self) do
        if type(value) == "number" then
            copy[key] = value
        end
    end
    return copy
end

return RuntimeMetrics
