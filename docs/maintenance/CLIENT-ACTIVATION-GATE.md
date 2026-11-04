# Gate da coordenadora antes de integrar/ativar o cliente

A manutenção nativa está ATIVA. Merge no fork pode acionar deploy. Esta frente
preparou somente código local; não executou comandos na VM nem alterou autonomia.

## Antes do merge

1. Revalidar main, os dois journals (`pending.json` e `client-pending.json`),
   serviço/timer de manutenção, cron/launchers externos e o estado do gateway.
   Não avançar com outra transação/poll em curso ou mudança concorrente sem dono.
2. A coordenadora adquire uma cerca temporária com retomada já armada ANTES de
   parar o timer. Registrar estado active/enabled e prazo. Exemplo de mecânica
   revisável, somente se o timer estava ativo:

   ```sh
   export XDG_RUNTIME_DIR=/run/user/1000
   export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
   systemd-run --user --unit=mcp-client-lease-resume-<id> --on-active=20m \
     /usr/bin/systemctl --user start mcp-maintenance-deploy.timer
   systemctl --user stop mcp-maintenance-deploy.timer
   ```

   Não usar disable/mask, não matar um poll em execução e não executar `--poll`
   manualmente. Esperar o oneshot concluir e confirmar lock livre antes de merge.
   Outros atores capazes de escrever a fonte também precisam de cerca com dono.
3. Enquanto esse lease existir, segurar merges/releases Go e dispatch manual.
   Este PR não muda `whatsapp-bridge/**`; `release.yml` agora só dispara por Go
   ou dispatch explícito. Portanto o merge exclusivo de cliente NÃO publica um
   novo pacote Go que o controlador antigo possa consumir. Revalidar isso no
   diff final; se a fila incorporar Go, este procedimento precisa de novo gate.

## Depois do merge, sem adiantar a fonte de produção

4. O workflow novo constrói/testa `mcp-client-release`. O controlador antigo
   ignora esse workflow e a fonte viva continua antiga. Não dar pull/FF/reset
   em `/home/thiago/repos/whatsapp-mcp` para instalar o controlador: isso exporia
   `.python-version`/lock novos ao UV legado antes da ativação.
5. Usar um checkout de INFRA separado, limpo e preso ao SHA integrado/revisado,
   para `python3 scripts/maintenance/install_executor.py --expected-sha <sha-integrado-revisado>`.
   O script recusa SHA diferente antes de qualquer escrita. Ele
   instala todos os módulos num diretório versionado completo, sob executor.lock,
   e só então publica a unit. Não instala Python/pacotes nem troca aplicativo.
   O bootstrap retoma o timer compatível, preservando a manutenção autônoma.
6. Confirmar unit apontando para a versão completa do controlador e timer ativo;
   encerrar o lease transitório quando a retomada tiver sido comprovada. Se o
   bootstrap não ocorrer antes do prazo, o timer antigo pode retomar porque a
   fila Go foi mantida cercada e o release exclusivo de cliente é ignorado por ele.
   Não liberar Go até confirmar o controlador compatível instalado.

## Primeira ativação

7. O controlador novo pode STAGE: verificar origem/CI/digests, extrair em namespace
   isolado, testar entrypoint real e publicar `client-stage.json`. Prova ausente
   não altera configuração nem para serviços. Stage é reaproveitado sem repetir
   download/canário a cada cinco minutos; os bytes locais continuam verificados.
8. Root escreve política regular/root-owned/sem escrita de grupo/outros (0644):

   ```json
   {"schema":1,"enabled":true,"repository":"thiavila/whatsapp-mcp",
    "workflow":".github/workflows/client-release.yml",
    "activation_sha":"<sha do primeiro client-stage>",
    "allow_automatic_managed_updates":true}
   ```

   Caminho: `/home/thiago/hermes-maintenance/mcp-deploy/client-policy.json`.
   A flag registra a autonomia já escolhida pelo usuário; não pede aprovação
   humana a cada release de cliente já migrado. Revogação explícita é `enabled:false`.
9. Antes da primeira migração UV → runner fixo, produzir `client-readiness.json`
   com valores de `client-stage` + checked_at/expires_at ≤1h e atestados reais de
   default+7 perfis e escritores externos cercados. Não preencher os booleanos
   por configuração, tools/list ou existência de token. A fonte viva ainda precisa
   ser pré-cliente; fonte adiantada com UV legado bloqueia, exigindo recuperação
   revisada em vez de fallback inseguro. Verificar eager start dos filhos do
   GATEWAY atual; ausência de baseline só permite stage, nunca cutover.
10. A retomada normal do timer promove quando todos os gates passarem, sem
    `executor --poll` manual. Confirmar código real dos filhos via /proc,
    ferramentas/prévias autorizadas, consumidores e saúde das três bridges.
    Nenhuma ferramenta de envio, QR, sessão, token ou banco deve ser alterado.

## Autonomia e recuperação

Atualizações posteriores de clientes já geridos não exigem nova prova humana
por payload. Exigem política inicial habilitada, versão anterior íntegra/real,
todos os comandos atuais já geridos, novo artefato admitido/canário e transação
guardada. Novo caller legado/novo perfil não recebe esse atalho. Os pins do
protocolo atual são explícitos; nova ABI/contrato exige revisão de infraestrutura.

O Go possui gate próprio antes do FF: enquanto houver UV legado ou transação
de cliente pendente, não pode expor novos inputs Python. Depois de migrados,
os clientes usam runtime/app imutáveis e um release Go não os altera.

Journal staged/refusa de stop sem mudança estrangeira de fonte: retomar estados anteriores, limpar journal e
repetir depois, sem bloquear SHA. Applying/applied: reconciliar campos realmente
trocados, recuperar OFFLINE pelo payload local e preservar todas as alterações
atuais de dados/credenciais. Fonte legada alterada por terceiro ou comando
estrangeiro mantém recuperação pendente com aviso único; nesse caso a retenção
dos serviços parados é intencional para não executar UV legado sobre fonte
inesperada. Nunca reset Git/DB/auth. Recusa por trabalho ativo durante rollback
aplicado retoma consumidores, mantém o journal e tenta novamente depois.
Rollback efetivo avisa uma vez e bloqueia só o candidato. Clientes e Go têm
tratamento de erro separado; bloqueio do cliente não encerra a fila inteira.
Go não exige a contagem histórica de filhos: wrappers geridos e todos os processos
presentes íntegros bastam, inclusive após remoção/lazy-start de uma entrada.
Todo adiamento Go avisa uma vez por SHA. Erros de cliente deduplicam por candidato,
tipo e motivo seguro, permitindo avisar falhas relevantes de releases posteriores.
