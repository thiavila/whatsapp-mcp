# Transição Indexa8080 — plano local e contrato de ativação

Base atual main dd4f7f95b4ec5b70ed3ed631ebb983162b7ccba3 (PR17 integrado).
Branch dedicada codex/indexa-bridge-auth-20261104. Não há GSD neste fork.
Outra frente controla cliente/procfix e a fila;
não alterar essas branches nem a VM. Root faz push/CI/merge/bootstrap/deploy.

## Evidência e escopo

Root provou3leituras SDK57/list_labels +GET equivalentes, Pythonreal3.11.15;
8080aceita semtoken,8083/84exigem e aceitam seus tokens. ProjeçãoRO própria às
01:04/01:11Z: unit Indexa tem binário direto/semflags, nenhum EnvironmentFile,
somente dropin media-cache. Um alvo Indexa no YAML: perfilindexa-roberto,
serverwhatsapp_indexa, porta default8080, sem inlineTOKEN/TOKEN_FILE. Outros
MCPs têm referências Melancia próprias. Nenhum corpo de segredo/cliente saiu.

Só uma reconciliação delimitada de arquivos/unidades/launchers reais é válida;
cache, backup e logs não constituem catálogo de consumidores. Referências
potenciais de administração wa-indexa-qr/wa-bridges-update precisam de cerca
durante o cutover; não são autorização para QR/logout/re-pair ou update manual.
Esta entrega fecha auth somente após ativação/prova real pela coordenadora.

## Desenho mínimo

1. Workflow oficial `indexa-auth-release.yml`, main/PR integrado/CI oficial,
   publica SOMENTE JSON tipado sem valores secretos e manifesto SHA. Não compila
   Go, não instala Python e não move banco. Artefato próprio não dispara como
   efeito de pacoteGo/cliente. Executor antigo ignora; bootstrap novo é versionado.
2. Root-owned policy escolhe APENAS token_ref indexa-rest e arquivo aprovado já
   existente/provisionado. O código não gera/rotaciona nem escolhe token por nome.
   Rejeitar referência compartilhada com Melancia e token inline desconhecido;
   proveniência Indexa e consumidores efetivos exigem atestado real fresco root.
3. Pré-condição: clientes já migrados ao runtime imutável gerido e íntegro. O
   adapter de YAML usa esse Python/PyYAML, sem dependência instalada na VM.
   Muda só env da entrada explicitamente autorizada: token inline vazio, arquivo
   aprovado e URL8080explícita. Não muda command/args, não serializa credenciais
   no journal; preserva bytes/valores alheios e recusa conflito de terceiros.
4. Unit recebe dropin próprio com ExecStart para um pequeno wrapper versionado
   que execve o MESMO binário Go/argv/workingdir. Depois de toda herança systemd,
   wrapper remove TOKEN inline e fixa TOKEN_FILE aprovado, AUTH_MODE=enforce e
   REQUIRE_TOKEN=true. Sem token válido/policy não sobe; jamais modo warn/off.
   As demais env/unidades/bridges e sessão/store ficam intactas.
5. Stage valida proveniência/manifesto/configuração, inventário e unitbaseline,
   política/tokenfile (valores só em memória), compatibilidade e prova root fresca.
   Sem prova/política/arquivo, só prepara; não para serviço nem escreve config.
6. Journal antes de parar qualquer consumidor; pausa timers, respeita workguard,
   para gateway/dashboard e exige ausência de filhos. Antes de parar a bridge
   aberta, grava fase arming irreversível; publica wrapper e confere ExecStart
   antes de declarar secure-hold. Dali em diante reboot também exige token.
   Configura clientes ANTES de iniciar bridge em enforce. Testa401sem/200com em
   endpoint protegido sem ler corpo; health HTTP200com projeção privada dos
   booleanos é necessário (health200sozinho não prova connected/logged_in).
   Retoma consumidores somente após todos os checks.
7. Recuperação offline usa journal/config/refs atuais. Antes de secure-hold,
   recusa/adiamento retoma estados e tenta depois. Depois, tenta completar
   enforce com a credencial ATUAL aprovada. Nunca restaura bytes de token, DB,
   sessão ou estado processado. Se houver configuração estrangeira/incompatível,
   mantém Indexa parada/cercada e avisa uma vez; nada de fallback permissivo.
   Esta entrega faz somente a primeira ativação. Não implementa troca posterior
   de política nem rollback para configuração segura anterior inexistente;
   esses casos exigem reconciliação explícita, jamais restauração aberta.
8. Mesmo lock externo; auth-pending é recuperado ANTES das filas Hermes/cliente/Go.
   Enquanto secure-hold pendente, outras filas não reiniciam consumidores ou
   alteram configuração. Autonomia retoma ao completar; ausência de ativação
   não desabilita manutenção existente e bloqueio não é silêncio indefinido.

## Arquivos previstos

- `ops/indexa-auth.json`, workflow e produtor de manifesto/configuração sem segredos.
- `scripts/maintenance/indexa_auth*.py`: admissão, policy/token validator,
  wrapper, adapterYAML/dropin, journal/provas/recovery e integração do polling.
- bootstrap com módulos completos/versionados e dependência de runtime gerido.
- testes sintéticos de401/200/health, token ausente/trocado/confusão Melancia,
  partialapply/refusas/crash/journal, conflito estrangeiro e nenhuma restauração
  de segredo/dados; revisão Opus5.5 proporcional, tetoUSD5.

UID fica fora. Nenhuma escrita/restart na VM, login, envio ou LLM de produto
por esta frente. Os únicos LLMs autorizados são as revisões independentes.

## Atualização de fatos e decisões

Root confirmou clientes geridos dd4f7f9 em produção, sem pending/blocked,
3 Python +3 UV íntegros e quatro leituras SDK reais antes da ativação. Não
reutilizar hashes YAML bca/UV legado nem FF no repositório vivo por isso.

RO delimitado no diretório `.config/whatsapp-bridges`: só os dois `.token`
Melancia conhecidos e seus `.conf`; nenhum Indexa ali. Não prova inexistência
em outros locais. Scripts ativos exatos `wa-bridges-update`, `wa-indexa-qr`,
`wa-indexa-watch` não contêm invocação direta detectável do binário; primeiros
dois usam systemctl, último SQLite/Hermes. Não executar nenhum no cutover.

YAML Indexa passa a explicitar URL/porta, token inline vazio e TOKEN_FILE.
Também recebe `UV_NO_ENV_FILE: "true"`: garante que dotenv não possa substituir
o contrato de auth. Root precisa comprovar que a entrada não depende de dotenv
para outros recursos antes de atestar `effective_environment_verified`.
Nenhum outro env, perfil, command/args ou valor de credencial é serializado.

RevisãoR1 apontou P1 recuperação validated sem reiniciar bridge, P2 falso vazio
SDK e P3 deferral silencioso/janela reboot. Corrigidos: start seguro+health antes
de reprovar validated; SDK também chama `list_joined_groups` somente leitura,
que preserva success:false/HTTP401 (57 ferramentas não mudam); erro pós-arming
propaga para notificação; secure-hold só existe depois de wrapper boot-safe.
O probe também recusa mudança do conteúdo atual do token durante a prova.

Implementação e sequência operacional: [INDEXA-AUTH-RUNBOOK.md](INDEXA-AUTH-RUNBOOK.md).
