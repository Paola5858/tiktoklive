local AvatarService = {}
AvatarService.__index = AvatarService

function AvatarService.new(config, metrics, cache, identityResolver, cleanupManager, effectService)
    return setmetatable({
        config = config,
        metrics = metrics,
        cache = cache,
        identityResolver = identityResolver,
        cleanupManager = cleanupManager,
        effectService = effectService,
        inFlight = {},
        spawnSequence = 0,
        spawnTimes = {},
    }, AvatarService)
end

function AvatarService:_getContainer()
    local container = workspace:FindFirstChild(self.config.containerName)
    if not container then
        container = Instance.new("Folder")
        container.Name = self.config.containerName
        container.Parent = workspace
    end
    return container
end

function AvatarService:_pruneSpawnTimes(now)
    local kept = {}
    for _, timestamp in ipairs(self.spawnTimes) do
        if timestamp > now - 1 then
            table.insert(kept, timestamp)
        end
    end
    self.spawnTimes = kept
end

function AvatarService:_allowSpawn()
    local now = os.clock()
    self:_pruneSpawnTimes(now)
    if #self.spawnTimes >= self.config.maxSpawnsPerSecond then
        self.metrics:record("droppedSpawnRequests")
        return false
    end
    table.insert(self.spawnTimes, now)
    return true
end

function AvatarService:_fallbackModel(displayName, position)
    local model = Instance.new("Model")
    model.Name = "LiveFallback_" .. displayName

    local root = Instance.new("Part")
    root.Name = "HumanoidRootPart"
    root.Size = Vector3.new(2, 3, 1)
    root.Color = self.config.fallbackColor
    root.Anchored = true
    root.CanCollide = false
    root.CFrame = CFrame.new(position)
    root.Parent = model

    local humanoid = Instance.new("Humanoid")
    humanoid.DisplayName = displayName
    humanoid.RequiresNeck = false
    humanoid.Parent = model
    model.PrimaryPart = root
    model:SetAttribute("LiveFallback", true)
    return model
end

function AvatarService:_lookupDescription(userId)
    local cached = self.cache:get(userId)
    if cached then
        self.metrics:record("avatarCacheHits")
        return cached, "cache"
    end
    self.metrics:record("avatarCacheMisses")

    if self.inFlight[userId] then
        local state = self.inFlight[userId]
        while not state.done do
            task.wait()
        end
        if state.ok then
            return state.value, state.source
        end
        return nil, "lookup_failed"
    end

    local state = { done = false, ok = false, value = nil, source = "lookup_failed" }
    self.inFlight[userId] = state
    local ok, description = pcall(function()
        return game:GetService("Players"):GetHumanoidDescriptionFromUserIdAsync(userId)
    end)
    if not ok then
        self.inFlight[userId] = nil
        self.metrics:record("avatarLookupFailures")
        state.done = true
        return nil, "lookup_failed"
    end
    self.cache:set(userId, description)
    state.ok = true
    state.value = description
    state.source = "lookup"
    self.inFlight[userId] = nil
    state.done = true
    return description, "lookup"
end

function AvatarService:_createModel(description)
    return game:GetService("Players"):CreateHumanoidModelFromDescriptionAsync(
        description,
        Enum.HumanoidRigType.R15,
        Enum.AssetTypeVerification.Default
    )
end

function AvatarService:_spawn(command)
    self.metrics:record("spawnRequests")
    if not self:_allowSpawn() then
        return nil, "spawn_rate_limited"
    end

    local priority = command.priority or "P3"
    if not self.cleanupManager:enforceLimit(priority) then
        self.metrics:record("resourceLimitHits")
        self.metrics:record("droppedSpawnRequests")
        return nil, "active_avatar_limit"
    end

    local actor = command.actor or {}
    local displayName = actor.display_name or "Live Viewer"
    local userId, identitySource = self.identityResolver:resolve(command)
    local position = self.config.spawnOrigin + self.config.spawnSpacing * (self.spawnSequence % 10)
    self.spawnSequence += 1

    local model
    local avatarSource = "fallback"
    if userId then
        local description = self:_lookupDescription(userId)
        if description then
            local ok, created = pcall(function()
                return self:_createModel(description)
            end)
            if ok and created then
                model = created
                avatarSource = "roblox_avatar"
            else
                self.metrics:record("avatarLookupFailures")
            end
        end
    end
    if not model then
        model = self:_fallbackModel(displayName, position)
        self.metrics:record("fallbackAvatarUsage")
    end

    model.Name = "LiveAvatar_" .. tostring(command.command_id or self.spawnSequence)
    model:SetAttribute("LiveEventId", tostring(command.source_event_id or command.command_id or "unknown"))
    model:SetAttribute("SourceUserId", tostring(actor.source_user_id or "unknown"))
    model:SetAttribute("AvatarSource", avatarSource)
    model:SetAttribute("IdentitySource", identitySource)
    model:SetAttribute("SpawnedAt", os.clock())
    model.Parent = self:_getContainer()
    model:PivotTo(CFrame.new(position))

    local lifetime = command.parameters and command.parameters.duration_seconds
        or (command.command_type == "GIFT_REACTION" and self.config.giftLifetimeSeconds or self.config.commentLifetimeSeconds)
    if type(lifetime) ~= "number" or lifetime <= 0 then
        lifetime = self.config.commentLifetimeSeconds
    end

    local recordId = tostring(command.command_id or self.spawnSequence)
    local expiresAt = os.clock() + lifetime
    model:SetAttribute("EventType", tostring(command.command_type or "UNKNOWN"))
    model:SetAttribute("ExpiresAt", expiresAt)
    self.cleanupManager:register(recordId, model, expiresAt, priority)
    self.metrics:record("spawnSuccess")

    local effectName = command.parameters and command.parameters.effect
    if effectName then
        self.effectService:apply(model, effectName, lifetime)
    end
    return model, avatarSource
end

function AvatarService:spawn(command)
    self.pendingSpawns = self.pendingSpawns or 0
    if self.pendingSpawns >= self.config.maxPendingSpawns then
        self.metrics:record("droppedSpawnRequests")
        return nil, "pending_spawn_limit"
    end
    self.pendingSpawns += 1
    local ok, model, reason = pcall(function()
        return self:_spawn(command)
    end)
    self.pendingSpawns -= 1
    if not ok then
        self.metrics:record("spawnFailure")
        return nil, "spawn_failed"
    end
    return model, reason
end

return AvatarService
