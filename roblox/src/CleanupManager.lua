local CleanupManager = {}
CleanupManager.__index = CleanupManager

function CleanupManager.new(maxActive, metrics, now)
    return setmetatable({
        maxActive = maxActive,
        metrics = metrics,
        now = now or os.clock,
        active = {},
        sequence = 0,
    }, CleanupManager)
end

function CleanupManager:_destroy(record)
    if not record or record.destroyed then
        return false
    end
    record.destroyed = true
    self.active[record.id] = nil
    local ok = pcall(function()
        if record.model and record.model.Parent then
            record.model:Destroy()
        end
    end)
    if ok then
        self.metrics:record("cleanupCount")
    else
        self.metrics:record("cleanupFailures")
    end
    self.metrics:setActiveAvatars(self:count())
    return ok
end

function CleanupManager:register(id, model, expiresAt, priority)
    self.sequence += 1
    local record = {
        id = id,
        model = model,
        expiresAt = expiresAt,
        priority = priority or "P3",
        createdSequence = self.sequence,
        destroyed = false,
    }
    self.active[id] = record
    self.metrics:setActiveAvatars(self:count())
    return record
end

function CleanupManager:remove(id)
    return self:_destroy(self.active[id])
end

function CleanupManager:count()
    local count = 0
    for _ in pairs(self.active) do
        count += 1
    end
    return count
end

function CleanupManager:cleanupExpired(now)
    now = now or self.now()
    local removed = 0
    for _, record in pairs(self.active) do
        if record.expiresAt <= now and self:_destroy(record) then
            removed += 1
        end
    end
    return removed
end

function CleanupManager:enforceLimit(incomingPriority)
    while self:count() >= self.maxActive do
        local candidate
        for _, record in pairs(self.active) do
            local protected = record.priority == "P0" or record.priority == "P1"
            if not protected or incomingPriority == "P0" then
                if not candidate or record.createdSequence < candidate.createdSequence then
                    candidate = record
                end
            end
        end
        if not candidate then
            return false
        end
        self:_destroy(candidate)
    end
    return true
end

function CleanupManager:destroyAll()
    local records = {}
    for _, record in pairs(self.active) do
        table.insert(records, record)
    end
    for _, record in ipairs(records) do
        self:_destroy(record)
    end
end

return CleanupManager
