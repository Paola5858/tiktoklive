# Testes do Roblox Avatar Runtime

Este checklist deve ser executado no Roblox Studio com um Bridge que entregue comandos já decodificados ao `LiveRuntime`. Os resultados precisam registrar Studio, mapa, número de comandos, tempo e contagem observada.

## Smoke test

1. Iniciar `LiveRuntime`.
2. Enviar `SPAWN_AVATAR` com `schema_version = "1.0"`, `command_id` único, `actor` fictício e `parameters.avatar_user_id = 1`.
3. Confirmar um único modelo em `Workspace.LiveActors`.
4. Confirmar os Attributes `LiveEventId`, `SourceUserId`, `AvatarSource`, `IdentitySource` e `SpawnedAt`.
5. Confirmar que o modelo possui `Humanoid` e `PrimaryPart`.

## Identidade e fallback

- Enviar comando sem `avatar_user_id` e sem mapping. Esperado: fallback, sem request externo.
- Enviar `avatar_user_id = 0`, string ou número fracionário. Esperado: rejeição segura ou fallback, sem lookup inválido.
- Simular falha de `GetHumanoidDescriptionFromUserIdAsync`. Esperado: fallback, métrica de falha e engine viva.
- Enviar dois comandos para o mesmo `source_user_id` com mapping configurado. Esperado: mapping usado, sem presumir que o nome exibido é conta Roblox.

## Idempotência

- Reenviar o mesmo `command_id`.
- Reenviar o mesmo `idempotency_key` com outro payload.
- Esperado: segundo comando rejeitado como duplicado e nenhuma segunda instância criada.

## Limites

- Enviar 500 `COMMENT_REACTION`. Esperado: nenhuma criação de avatar por padrão.
- Enviar burst de `GIFT_REACTION`. Esperado: limite de spawns por segundo e limite de avatares ativos respeitados.
- Confirmar que a contagem máxima não ultrapassa o valor configurado.
- Confirmar que P0/P1 não são expulsos por candidatos comuns quando a política receber esses metadados.

## TTL e cleanup

- Usar TTL curto de teste, criar avatar e aguardar expiração.
- Confirmar `Model:Destroy()` e redução de `activeAvatars`.
- Encerrar runtime com modelos e efeitos ativos. Confirmar que ambos são destruídos.
- Chamar `stop()` duas vezes. Esperado: sem erro e sem referências temporárias deixadas pelo runtime.

## Efeitos

- Testar `HEARTS`, `GOLD_AURA` e `BLUE_GLOW`.
- Testar nome de efeito arbitrário. Esperado: rejeição do efeito sem criação de asset ou script.
- Confirmar que `activeEffects` volta a zero após TTL e shutdown.

## Experimento obrigatório do transporte

Separar estes resultados do runtime:

- Roblox Studio Play Solo alcança a API local?
- Team Test apresenta o mesmo resultado?
- Jogo publicado alcança localhost? Não inferir a resposta do Studio.
- Qual a latência e taxa de erro do polling?

Nenhum desses resultados foi medido no ambiente atual.
