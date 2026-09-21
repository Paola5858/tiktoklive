# Roblox Avatar Runtime

## Estado da implementação

Esta fase implementa o runtime de gameplay que recebe um `command` já decodificado pelo Roblox Bridge. O runtime não conhece TikTok, não interpreta texto como código e não escolhe prioridade. Ele valida o contrato, aplica idempotência, roteia para handlers e controla o lifecycle dos modelos temporários.

O repositório atualizado não contém um Bridge de transporte da fase anterior. Portanto, `init.server.lua` deixa explícito o ponto de integração `runtime:handle(decodedCommand)`, mas não afirma que o Roblox Studio já alcança a API local. Esse experimento continua pendente e deve ser executado separadamente.

## Arquitetura

```text
Roblox Bridge, futuro transporte
        |
        v
LiveRuntime.handle(command)
        |
        v
GameEventRouter, allowlist e schema
        |
        +--> CommentReaction handler
        +--> GiftReaction handler
        +--> SpawnAvatar handler
                    |
                    v
             AvatarService
              |       |
       Identity    AvatarCache
       Resolver        |
              |        v
              +--> Roblox Players API
                    |
                    v
             CleanupManager + EffectService
```

Os módulos são separados por responsabilidade. O router não chama APIs de transporte, o AvatarService não sabe de TikTok e os handlers não carregam regras de prioridade.

## GameEvents implementados

| `command_type` | Comportamento |
|---|---|
| `SPAWN_AVATAR` | Resolve uma identidade explícita ou usa fallback, cria o modelo e registra TTL. |
| `COMMENT_REACTION` | Apenas reconhece o comando. Comentário não cria avatar por padrão, evitando flood de entidades. |
| `GIFT_REACTION` | Usa o mesmo AvatarService com TTL de gift e efeito opcional allowlisted. |

Os tipos futuros do schema, como `SHOW_MESSAGE`, `REMOVE_ENTITY` e `APPLY_EFFECT`, são rejeitados até possuírem handler real. Isso é intencional: aceitar um comando sem comportamento verificável é uma forma elegante de criar bug invisível.

## APIs Roblox verificadas

A implementação usa as APIs atuais documentadas no Creator Hub:

- `Players:GetHumanoidDescriptionFromUserIdAsync(userId)`, que retorna `HumanoidDescription` e pode falhar para usuário inválido ou indisponível.
- `Players:CreateHumanoidModelFromDescriptionAsync(description, rigType, assetTypeVerification)`, que retorna `Model`.
- `Instance:SetAttribute`, `Instance:GetAttribute` e `Instance:Destroy` para metadados e cleanup.
- `Model:PivotTo(CFrame)` para posicionamento.
- `Instance.new` para `Folder`, `Model`, `Part`, `Humanoid` e `Highlight`.
- `Debris:AddItem` foi verificado, mas o runtime usa `CleanupManager` como fonte principal de lifecycle para manter contagem, prioridade e idempotência sob controle. O Debris não substitui o registro interno.

Não foram usados os métodos sem sufixo `Async` porque a documentação atual os marca como deprecated.

## Identidade

O runtime não presume que o username TikTok seja um username Roblox. A ordem de resolução é:

1. `parameters.avatar_user_id`, quando o comando traz um número Roblox inteiro positivo.
2. Mapeamento configurado `source_user_id -> roblox_user_id`.
3. Modelo fallback genérico, sem chamada de rede.

Se não houver uma associação confiável, a live continua funcionando com uma representação simples. Nenhum lookup de nome Roblox é inventado.

## Cache e lookup concorrente

`AvatarCache` tem limite de 256 entradas por padrão, TTL de 900 segundos e eviction por TTL/LRU. A chave é o `roblox_user_id`, não o nome exibido na live. O `AvatarService` mantém um estado in-flight por identidade para que chamadas simultâneas compartilhem o resultado de uma única consulta.

As métricas expostas incluem hits, misses e falhas de lookup. O cache não é persistente e não cresce indefinidamente.

## Spawn e lifecycle

Cada modelo recebe Attributes mínimos para diagnóstico:

- `LiveEventId`;
- `SourceUserId`;
- `AvatarSource`;
- `IdentitySource`;
- `SpawnedAt`;
- `EventType`;
- `ExpiresAt`.

Defaults experimentais:

- máximo de 100 avatares ativos;
- comentário: 60 segundos;
- gift: 300 segundos;
- máximo de 100 spawns pendentes;
- máximo de 10 spawns por segundo;
- container dedicado `Workspace.LiveActors`.

Esses números são configuração inicial, não capacidade comprovada do Roblox. Quando o limite ativo é atingido, o runtime remove o registro mais antigo não protegido. Registros P0/P1 são protegidos contra expulsão por entradas comuns; se não houver candidato seguro, o spawn é rejeitado.

## Fallback

O fallback é um `Model` pequeno com `Part` ancorado, `Humanoid` e `HumanoidRootPart`. Ele não depende de rede, catálogo, asset ID ou identidade Roblox. O uso do fallback é contado em `fallbackAvatarUsage`.

## Efeitos

A allowlist atual contém apenas efeitos locais sem asset externo:

- `HEARTS`;
- `GOLD_AURA`;
- `BLUE_GLOW`.

Eles são implementados com `Highlight` e têm lifetime próprio. Asset IDs, scripts externos e conteúdo arbitrário do TikTok não são aceitos.

## Cleanup

O `CleanupManager` registra cada modelo, associa `expiresAt`, remove expirados a cada segundo e destrói tudo no shutdown. O método de destruição é idempotente e mede falhas. O `EffectService` mantém os `Highlight` ativos separados e também os destrói no shutdown.

## Métricas

`LiveRuntime:metricsSnapshot()` expõe contadores de:

- active/peak avatars;
- spawn requests, sucesso, falha e descartes;
- cache hit/miss;
- falhas de lookup;
- fallback;
- cleanup e falhas de cleanup;
- efeitos ativos;
- comandos duplicados/rejeitados;
- limites de recurso atingidos.

## Limitações conhecidas

Não há runtime Luau ou Roblox Studio disponível no ambiente de desenvolvimento para executar os módulos. A sintaxe foi revisada estaticamente, as APIs foram verificadas no Creator Hub, mas a criação efetiva de modelo, a aparência do avatar e a latência não foram medidas nesta sessão. A integração HTTP do Bridge, o alcance de localhost no Studio e o comportamento em jogo publicado continuam experimentos obrigatórios.
