local EffectService = {}
EffectService.__index = EffectService

local EFFECTS = {
    HEARTS = { color = Color3.fromRGB(255, 90, 150), brightness = 2 },
    GOLD_AURA = { color = Color3.fromRGB(255, 210, 60), brightness = 3 },
    BLUE_GLOW = { color = Color3.fromRGB(80, 170, 255), brightness = 2 },
}

function EffectService.new(metrics)
    return setmetatable({ metrics = metrics, active = {} }, EffectService)
end

function EffectService:apply(model, effectName, lifetimeSeconds)
    local definition = EFFECTS[effectName]
    if not definition or not model or not model.Parent then
        return false, "unknown_effect"
    end

    local highlight = Instance.new("Highlight")
    highlight.Name = "LiveEffect_" .. effectName
    highlight.FillColor = definition.color
    highlight.OutlineColor = definition.color
    highlight.FillTransparency = 0.65
    highlight.OutlineTransparency = 0.1
    highlight.Adornee = model
    highlight.Parent = model
    table.insert(self.active, highlight)
    self.metrics:record("activeEffects")

    task.delay(lifetimeSeconds, function()
        self:remove(highlight)
    end)
    return true
end

function EffectService:remove(effect)
    for index, activeEffect in ipairs(self.active) do
        if activeEffect == effect then
            table.remove(self.active, index)
            self.metrics:record("activeEffects", -1)
            break
        end
    end
    pcall(function()
        if effect and effect.Parent then
            effect:Destroy()
        end
    end)
end

function EffectService:destroyAll()
    local effects = self.active
    self.active = {}
    for _, effect in ipairs(effects) do
        pcall(function()
            if effect.Parent then
                effect:Destroy()
            end
        end)
    end
    self.metrics.activeEffects = 0
end

return EffectService
