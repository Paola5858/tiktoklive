# EVENT_SCHEMA.md — schema de eventos internos

## regra central

O Roblox consome comandos abstratos. Ele não conhece TikTok, nomes de gifts, regras de prioridade ou formatos de payload externos. A tradução acontece antes da fronteira do bridge.

Este schema é conceitual — os tipos exatos (int vs string, formato de timestamp) se fecham na implementação, não aqui.

---

## evento interno normalizado (pós-normalizer)

```json
{
  "schema_version": "1.0",
  "event_id": "01J...",
  "event_type": "COMMENT | GIFT | FOLLOW | SHARE | LIKE | SYSTEM | MANUAL | CUSTOM",
  "source": "tiktok",
  "occurred_at": "ISO-8601 (quando a origem afirma que ocorreu)",
  "received_at": "ISO-8601 (quando a engine recebeu)",
  "actor": {
    "source_user_id": "id do usuário na origem",
    "display_name": "nome exibido"
  },
  "priority": "P0 | P1 | P2 | P3 | P4",
  "dedupe_key": "tiktok:gift:external-id:gift-id:repeat-1",
  "payload": { "...": "específico do event_type" },
  "status": "received | normalized | accepted | queued | processing | aggregated | dropped | processed | failed"
}
```

### campos e invariantes

| Campo | Tipo conceitual | Regra |
|---|---|---|
| `schema_version` | string | obrigatório e compatível com consumidores |
| `event_id` | string | único por evento aceito; não confiar cegamente no ID externo |
| `event_type` | enum | `COMMENT`, `GIFT`, `FOLLOW`, `SHARE`, `LIKE`, `SYSTEM`, `MANUAL`, `CUSTOM` |
| `source` | enum/string | identifica a origem, nunca determina a mecânica do Roblox |
| `occurred_at` | timestamp | quando a origem afirma que ocorreu |
| `received_at` | timestamp | quando a engine recebeu |
| `actor` | objeto opcional | conter apenas identidade necessária |
| `priority` | enum | calculada pelo event engine: `P0` a `P4` |
| `dedupe_key` | string opcional | usada dentro da janela de deduplicação |
| `payload` | objeto | validado por tipo e com limite de tamanho |
| `status` | enum | progressão do ciclo de vida do evento |

Timestamps devem ser tratados com timezone explícito. No modelo Python atual, `Event.timestamp` representa `occurred_at` e `Event.received_at` representa o instante de ingestão. O connector preserva `CommonMessageData.create_time` quando disponível e usa o horário de recebimento somente como fallback explícito. Payloads desconhecidos ou grandes devem ser rejeitados ou reduzidos de forma observável. Falha de normalização nunca deve produzir um comando Roblox parcial.

### payload por tipo (conceitual)

- **GIFT**: `{ gift_id, gift_value, repeat_count }`
- **COMMENT**: `{ text }`
- **FOLLOW**: `{}` (o evento em si já carrega a informação)
- **SHARE / LIKE**: `{ count }` quando aplicável (like costuma vir agregado por natureza)
- **SYSTEM**: `{ reason }` — ex: `live_started`, `live_ended`, `reconnecting`
- **MANUAL**: `{ triggered_by, action }` — comando manual do creator/operador
- **CUSTOM**: aberto, pra fontes futuras

---

## modelo de prioridade

| nível | significado |
|---|---|
| P0 | sistema / controle crítico (start/stop, comando de operador) |
| P1 | presentes e eventos de alto valor |
| P2 | comandos especiais |
| P3 | comentários normais |
| P4 | eventos descartáveis / agregáveis |

A prioridade é atribuída pelo EVENT_ENGINE, nunca pelo Roblox. O Roblox recebe o evento já com prioridade resolvida. A prioridade é uma decisão da engine, baseada em política configurável — não em lógica Luau.

---

## comando abstrato para o Roblox (pós EVENT_ENGINE) — formato-alvo, fase 5

**Nota (fase 4):** o que atravessa a Local API HOJE não é este formato — é o
`GameEventEnvelope` implementado em `src/adapters/roblox.py` e documentado
em `context/ROBLOX_BRIDGE.md` (schema_version "1.0" também, mas com campos
diferentes: `sequence_number`, `event_type` ainda como `EventType`/`"AGGREGATED"`
do domínio, sem `command_type`/`parameters` de gameplay). Este `Command`
abaixo é o formato-alvo de quando o Gift Mapping Engine (fase 5) existir e
decidir a tradução gift → efeito. `src/domain/commands.py` já define o
dataclass `Command`/`CommandType` pra esse futuro, mas nada ainda o
constrói de verdade.

Esse é o formato que deve atravessar a LOCAL_API até o Roblox quando essa tradução existir — já traduzido, sem vestígio da origem:

```json
{
  "schema_version": "1.0",
  "command_id": "01J...",
  "command_type": "SPAWN_AVATAR | APPLY_EFFECT | REMOVE_ENTITY | SHOW_MESSAGE | SYSTEM_STATUS | SYSTEM_SIGNAL",
  "created_at": "ISO-8601",
  "priority": "P0..P4",
  "source_event_id": "01J...",
  "actor": {
    "source_user_id": "external-id",
    "display_name": "example_user"
  },
  "parameters": {
    "avatar_user_id": 123456,
    "scale": 1.2,
    "effect": "HEARTS",
    "duration_seconds": 60
  },
  "idempotency_key": "spawn:source-event-id",
  "expires_at": "ISO-8601"
}
```

**Nota importante:** `actor.source_user_id` aqui é o ID na origem (TikTok). A resolução pra um usuário/avatar do Roblox é uma etapa separada — ver `DECISIONS.md` (resolução de identidade TikTok → Roblox). O conjunto real de `command_type` deve ser pequeno até as mecânicas serem validadas.

---

## gift mapping (config, não código)

Mappings devem ficar em configuração validada, não espalhados pelo código:

```yaml
rose:
  command_type: SPAWN_AVATAR
  scale: 1.2
  effect: HEARTS
  duration_seconds: 60
lion:
  command_type: SPAWN_AVATAR
  scale: 2.0
  effect: GOLD_AURA
  duration_seconds: 120
```

A configuração deve rejeitar: command_types não permitidos, durações negativas, escalas fora de limite e efeitos desconhecidos. O exemplo é ilustrativo e não prova que esses gifts existam ou tenham esses significados na plataforma.

Vive em `/configs`, versionado separado do código. O EVENT_ENGINE lê essa tabela pra montar o comando — nenhuma regra de gift específica hardcoded em `.py` ou `.lua`.

---

## compatibilidade e versionamento

Mudanças incompatíveis devem alterar `schema_version` e ser registradas em `DECISIONS.md`. Consumidores devem rejeitar versões desconhecidas de forma explícita. Campos novos devem ser opcionais quando possível. Nunca remover um campo usado pelo Roblox sem plano de migração.

---

## registro em disco (JSONL, `/events/YYYY-MM-DD.jsonl`)

```json
{ "timestamp": "...", "event_type": "GIFT", "user": "...", "gift": "...", "processed": true, "latency_ms": 142 }
```

**Regra de privacidade:** registrar só o necessário pra debug/replay. Sem dados sensíveis além do que já é público na live (nome de usuário exibido). O contrato não deve carregar texto integral de comentário, avatar description ou tokens de autenticação por padrão. Se um campo for necessário para depuração, aplicar minimização, retenção definida e redaction nos logs.
