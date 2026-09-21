# Interaction Rules Engine

## Objetivo

A camada de interação fica entre o evento normalizado e o gameplay. O connector TikTok só normaliza dados. O Roblox Runtime só executa comandos allowlisted. Nenhuma dessas duas camadas decide que um gift específico deve causar um efeito. Essa decisão vive em regras declarativas versionadas.

O fluxo implementado é:

```text
Event normalizado
    -> InteractionConsumer
    -> InteractionRuleEngine
    -> ActionDefinition / ActionResult
    -> GameEventFactory
    -> RobloxBridge.publish_game_event
    -> envelope GAME_COMMAND
    -> BridgeClient
    -> LiveRuntime
```

## Regra declarativa

A configuração é um objeto JSON/YAML equivalente a este formato:

```json
{
  "id": "gift_by_stable_id",
  "enabled": true,
  "event_type": "GIFT",
  "match": {
    "gift_id": 123
  },
  "actions": [
    {
      "type": "SPAWN_AVATAR",
      "params": {
        "duration_seconds": 60,
        "effect": "HEARTS"
      }
    }
  ],
  "priority": "P1",
  "cooldown": {
    "scope": "user",
    "seconds": 3
  },
  "aggregation": {
    "window_seconds": 0,
    "threshold": 0,
    "key": "event_type"
  },
  "stop_processing": false
}
```

O `gift_id` é preferível quando a fonte fornece um identificador estável. `gift_name` é aceito para configuração legível, mas não é tratado como único nem como prova de valor. O arquivo de exemplo usa `example_gift` e está documentado como fictício. A fase não inventa valor em coins, preço, streak ou semântica de quantidade.

A biblioteca normalizadora atual fornece `gift_id`, `gift_name`, `repeat_count`, `combo_count` e `repeat_end`. A regra usa `repeat_count` somente quando `min_quantity` foi configurado. Nenhuma lógica de streak foi implementada porque o contrato atual não prova início, continuidade e encerramento de streak de maneira suficiente para não duplicar ações.

## Matching

O engine suporta `event_type`, `gift_id`, `gift_name`, palavra-chave de comentário, `case_sensitive`, `min_quantity` e `user_external_id`. O texto de comentário é tratado como dado: há normalização case-insensitive opcional, mas não há regex arbitrária, parser, eval, Lua, Python ou execução dinâmica.

O campo de usuário é uma condição explícita de configuração. A camada não confia em um atributo recebido do TikTok para declarar VIP, moderador ou qualquer privilégio.

## Actions e compatibilidade Roblox

A representação intermediária é `ActionDefinition`, seguida de `ActionResult` e então `GameEvent`. Cada ação conserva `action_id`, `source_event_id`, `rule_id`, prioridade, timestamps e parâmetros.

Nesta fase, o único `action_type` habilitado é `SPAWN_AVATAR`, porque esse é o único comportamento de gameplay realmente conectado ao runtime atual. `PLAY_EFFECT` aparece como conceito futuro, mas o efeito já pode ser um parâmetro allowlisted de `SPAWN_AVATAR`. `SHOW_MESSAGE` e `UPDATE_COUNTER` são rejeitados até existirem handlers reais no Roblox. Aceitar ações sem consumidor seria apenas fabricar uma sensação de progresso.

O `GameEventFactory` produz um payload com `schema_version`, `command_id`, `command_type`, `created_at`, `priority`, `source_event_id`, `actor`, `parameters`, `idempotency_key` e `expires_at`. O bridge o entrega como envelope `event_type = GAME_COMMAND`; o `BridgeClient` encaminha o payload ao `LiveRuntime`, que faz a validação final e a allowlist de gameplay.

## Cooldown, dedupe e rate limit

O cooldown possui escopos `rule`, `gift`, `user` e `global`, com duração máxima de 24 horas. O estado é bounded e tem TTL. O dedupe usa a combinação do evento original, regra, tipo de ação e identidade declarativa da ação. Assim, a repetição do mesmo evento não duplica o efeito, enquanto dois eventos diferentes com o mesmo gift continuam sendo eventos diferentes.

O rate limiter possui limites globais e por usuário por segundo. A configuração inicial é 100 ações globais e 20 por usuário. Ações P0 não são descartadas pelo limite comum; P1 e prioridades menores podem ser bloqueadas quando o orçamento está cheio. O limite existe além da fila do Event Engine, porque produzir ações infinitas antes de enfileirar também seria um problema.

## Agregação

A agregação é opt-in por regra. Uma regra pode acumular eventos equivalentes por `event_type`, `gift_id`, `keyword` ou `user`, dentro de uma janela de até 300 segundos e com threshold bounded. Enquanto o threshold não é atingido, nenhuma ação é criada. Gifts não são agregados por padrão, porque a unidade individual pode ter significado próprio. Não há combo implementado nesta fase: sem uma mecânica real no produto, criar estado de combo seria complexidade decorativa.

## Expiração e isolamento

Ações com `duration_seconds` produzem `expires_at`. A factory recusa uma ação já expirada, e o consumer não publica GameEvents expirados. O Roblox Runtime faz sua própria limpeza dos modelos. Essa defesa em camadas evita que uma ação atrasada do buffer continue relevante depois de perder o prazo.

Falhas de configuração acontecem cedo: tipos desconhecidos, durações negativas, números fora de limite, prioridades inválidas, cooldown inválido, ações demais, campos demais e eventos incompatíveis são rejeitados. Falhas de um handler ou consumer continuam isoladas pela arquitetura de consumers do Event Engine.

## Configuração atual

O arquivo versionado é `configs/interaction_rules.json`. Ele é um exemplo neutro e não afirma que `example_gift` existe na plataforma. A aplicação deve carregar esse arquivo por composição explícita e criar `InteractionRuleEngine`; hot reload, editor visual, banco remoto, SaaS e painel não fazem parte desta fase.

## Métricas

`RuleMetrics` mede regras avaliadas e casadas, rejeições, ações criadas e descartadas, cooldown hits, rate limit hits, dedupe hits, agregações criadas e ações expiradas. O objetivo é tornar visível quando a regra não casou, quando foi bloqueada e quando o consumidor não acompanhou, sem logar o payload completo de cada comentário.

## Limitações e próximas fases

Ainda não existe uma composição única que inicialize TikTok, Event Engine, InteractionConsumer, RobloxBridge e Local API no mesmo processo. A ponte já aceita `GAME_COMMAND`, mas a montagem do processo continua uma decisão separada. Também não há handlers reais para mensagens, contadores, animações, OBS ou MQTT. Combos, ranking, edição remota e valores econômicos de gifts devem esperar dados de live e uma mecânica de produto definida.
