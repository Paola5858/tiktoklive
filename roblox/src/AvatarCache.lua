local AvatarCache = {}
AvatarCache.__index = AvatarCache

function AvatarCache.new(maxEntries, ttlSeconds, now)
    assert(maxEntries > 0, "maxEntries must be positive")
    assert(ttlSeconds > 0, "ttlSeconds must be positive")
    return setmetatable({
        maxEntries = maxEntries,
        ttlSeconds = ttlSeconds,
        now = now or os.clock,
        entries = {},
        size = 0,
    }, AvatarCache)
end

function AvatarCache:_remove(key)
    if self.entries[key] then
        self.entries[key] = nil
        self.size -= 1
    end
end

function AvatarCache:_evictExpired(now)
    for key, entry in pairs(self.entries) do
        if entry.expiresAt <= now then
            self:_remove(key)
        end
    end
end

function AvatarCache:get(key)
    local entry = self.entries[key]
    if not entry then
        return nil
    end
    local now = self.now()
    if entry.expiresAt <= now then
        self:_remove(key)
        return nil
    end
    entry.lastUsedAt = now
    return entry.value
end

function AvatarCache:set(key, value)
    local now = self.now()
    self:_evictExpired(now)
    if self.entries[key] then
        self.entries[key] = {
            value = value,
            createdAt = self.entries[key].createdAt,
            lastUsedAt = now,
            expiresAt = now + self.ttlSeconds,
        }
        return
    end
    while self.size >= self.maxEntries do
        local oldestKey
        local oldestTime = math.huge
        for candidate, entry in pairs(self.entries) do
            if entry.lastUsedAt < oldestTime then
                oldestKey = candidate
                oldestTime = entry.lastUsedAt
            end
        end
        if not oldestKey then
            break
        end
        self:_remove(oldestKey)
    end
    self.entries[key] = {
        value = value,
        createdAt = now,
        lastUsedAt = now,
        expiresAt = now + self.ttlSeconds,
    }
    self.size += 1
end

function AvatarCache:clear()
    self.entries = {}
    self.size = 0
end

return AvatarCache
