local GameEventRouter = {}
GameEventRouter.__index = GameEventRouter

local ALLOWED_TYPES = {
    SPAWN_AVATAR = true,
    COMMENT_REACTION = true,
    GIFT_REACTION = true,
}

function GameEventRouter.new(handlers, metrics)
    return setmetatable({ handlers = handlers, metrics = metrics }, GameEventRouter)
end

function GameEventRouter:route(command)
    if type(command) ~= "table" then
        self.metrics:record("rejectedCommands")
        return false, "command_not_table"
    end
    if command.schema_version ~= "1.0" then
        self.metrics:record("rejectedCommands")
        return false, "unsupported_schema_version"
    end
    if type(command.command_type) ~= "string" or not ALLOWED_TYPES[command.command_type] then
        self.metrics:record("rejectedCommands")
        return false, "unsupported_command_type"
    end
    if type(command.command_id) ~= "string" or #command.command_id == 0 or #command.command_id > 128 then
        self.metrics:record("rejectedCommands")
        return false, "invalid_command_id"
    end
    if type(command.parameters) ~= "table" then
        self.metrics:record("rejectedCommands")
        return false, "invalid_parameters"
    end
    if command.actor ~= nil and type(command.actor) ~= "table" then
        self.metrics:record("rejectedCommands")
        return false, "invalid_actor"
    end

    local handler = self.handlers[command.command_type]
    if not handler then
        self.metrics:record("rejectedCommands")
        return false, "handler_not_registered"
    end
    local ok, result, detail = pcall(handler, command)
    if not ok then
        self.metrics:record("spawnFailure")
        return false, "handler_failed"
    end
    return result ~= nil and result ~= false, detail
end

return GameEventRouter
