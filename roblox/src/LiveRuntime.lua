local RuntimeConfig = require(script.Parent.RuntimeConfig)
local RuntimeMetrics = require(script.Parent.RuntimeMetrics)
local AvatarCache = require(script.Parent.AvatarCache)
local IdentityResolver = require(script.Parent.IdentityResolver)
local CleanupManager = require(script.Parent.CleanupManager)
local EffectService = require(script.Parent.EffectService)
local AvatarService = require(script.Parent.AvatarService)
local Handlers = require(script.Parent.Handlers)
local GameEventRouter = require(script.Parent.GameEventRouter)

local LiveRuntime = {}
LiveRuntime.__index = LiveRuntime

function LiveRuntime.new(overrides, identityMapping)
    local config = RuntimeConfig.new(overrides)
    local metrics = RuntimeMetrics.new()
    local cache = AvatarCache.new(config.avatarCacheMaxEntries, config.avatarCacheTtlSeconds)
    local identityResolver = IdentityResolver.new(identityMapping)
    local cleanupManager = CleanupManager.new(config.maxActiveAvatars, metrics)
    local effectService = EffectService.new(metrics)
    local avatarService = AvatarService.new(config, metrics, cache, identityResolver, cleanupManager, effectService)
    local handlers = Handlers.new(avatarService, effectService)

    return setmetatable({
        config = config,
        metrics = metrics,
        cache = cache,
        cleanupManager = cleanupManager,
        effectService = effectService,
        router = GameEventRouter.new(handlers, metrics),
        seen = {},
        seenOrder = {},
        running = false,
        cleanupTask = nil,
    }, LiveRuntime)
end

function LiveRuntime:_remember(key)
    local now = os.clock()
    local entry = self.seen[key]
    if entry and entry > now then
        return false
    end
    self.seen[key] = now + self.config.idempotencyTtlSeconds
    table.insert(self.seenOrder, key)
    while #self.seenOrder > self.config.idempotencyCacheMaxEntries do
        local oldest = table.remove(self.seenOrder, 1)
        self.seen[oldest] = nil
    end
    return true
end

function LiveRuntime:handle(command)
    if type(command) ~= "table" then
        self.metrics:record("rejectedCommands")
        return false, "command_not_table"
    end
    local key = command and (command.idempotency_key or command.command_id)
    if type(key) ~= "string" or #key == 0 or #key > 160 then
        self.metrics:record("rejectedCommands")
        return false, "invalid_idempotency_key"
    end
    if not self:_remember(key) then
        self.metrics:record("duplicateCommands")
        return false, "duplicate_command"
    end
    return self.router:route(command)
end

function LiveRuntime:start()
    if self.running then
        return
    end
    self.running = true
    self.cleanupTask = task.spawn(function()
        while self.running do
            self.cleanupManager:cleanupExpired()
            task.wait(1)
        end
    end)
end

function LiveRuntime:stop()
    if not self.running then
        return
    end
    self.running = false
    -- Cancela o thread de cleanup em vez de apenas nil a referência.
    -- task.cancel é seguro mesmo se o thread já terminou.
    if self.cleanupTask then
        task.cancel(self.cleanupTask)
        self.cleanupTask = nil
    end
    self.effectService:destroyAll()
    self.cleanupManager:destroyAll()
end

function LiveRuntime:metricsSnapshot()
    local snapshot = self.metrics:snapshot()
    snapshot.activeAvatars = self.cleanupManager:count()
    snapshot.cacheEntries = self.cache.size
    return snapshot
end

return LiveRuntime
