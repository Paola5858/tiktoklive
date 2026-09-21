local Handlers = {}

function Handlers.new(avatarService, effectService)
    local handlers = {}

    handlers.SPAWN_AVATAR = function(command)
        return avatarService:spawn(command)
    end

    handlers.COMMENT_REACTION = function(command)
        -- Comentário não cria avatar por padrão. Um efeito configurado pode
        -- ser aplicado somente a uma entidade já existente no futuro.
        return true, "comment_acknowledged"
    end

    handlers.GIFT_REACTION = function(command)
        return avatarService:spawn(command)
    end

    return handlers
end

return Handlers
