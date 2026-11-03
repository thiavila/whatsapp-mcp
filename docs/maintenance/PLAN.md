# 2026-11-03 — CI leve e atualização autônoma

Thiago autorizou CI de código, manutenção autônoma pelo Hermes default na VM e aviso Telegram. Em 03/11 dispensou revisor por enquanto. Nada depende do Mac na execução agendada.

1. CI Go/Python rápida e isolada, caches persistentes, uma compilação via go test, sem Docker/serviços/LLM/dados reais. Atestado do commit limpo, CI oficial verifica conta/SHA/base.
2. Pipeline oficial de release, acionado somente depois do merge com CI aprovado. Publicar artefato de fonte do commit main; executor externo ao gateway valida origem/commit/digest e prepara antes da parada.
3. Promover bridges uma a uma com backup SQLite consistente, preservar sessão/estado e verificar saúde, abortar/rollback sem restaurar auth antigo. Dependências/schema incompatíveis bloqueiam autopromoção.
4. Integrar o executor e gates à política do Hermes; testes sintéticos e canário real sem mudança funcional antes de habilitar promoção; comunicar conclusão/bloqueio Telegram.

A frente de segurança Hermes já possui candidato novembro em outra worktree: não sobrescrever ou reativar pacote outubro obsoleto. Integrar apenas contrato de executor, sem competir com a fila.

## Escopo final do executor

O mesmo serviço separado do gateway recebe dois tipos de pipeline oficial:
MCP VM release para binário Go e Hermes official installer request para recibo
local de candidato+testes e release oficial de pelo menos 72h. O segundo usa
os estágios products/complete do instalador upstream, sem copiar aplicativo
por SCP. Não substitui o pipeline de hardening de novembro de outra frente.
Não promove patch da mesma versão ou downgrade automaticamente. O canário
`action=check` valida o caminho real sem troca desnecessária do runtime atual.

O usuário dispensou revisor em 03/11; não há instalação de Claude/API/fallback.
