local IdentityResolver = {}
IdentityResolver.__index = IdentityResolver

function IdentityResolver.new(mapping)
    return setmetatable({ mapping = mapping or {} }, IdentityResolver)
end

function IdentityResolver:resolve(command)
    local params = command.parameters or {}
    local explicitId = params.avatar_user_id
    if explicitId ~= nil then
        if type(explicitId) ~= "number" or explicitId <= 0 or explicitId % 1 ~= 0 then
            return nil, "invalid_avatar_user_id"
        end
        return explicitId, "explicit_mapping"
    end

    local sourceUserId = command.actor and command.actor.source_user_id
    if sourceUserId and self.mapping[sourceUserId] then
        local mapped = self.mapping[sourceUserId]
        if type(mapped) == "number" and mapped > 0 and mapped % 1 == 0 then
            return mapped, "configured_mapping"
        end
    end

    return nil, "fallback"
end

return IdentityResolver
