# Entrega local Indexa auth — 2026-11-03 (UTC 2026-11-04)

Implementação OpenAI; revisão independente Claude Opus5.5, modelo efetivo
confirmado nos dois recibos originais, perfil Pessoal. Base dd4f7f95b4ec5b70ed3ed631ebb983162b7ccba3.

**CODE GO R2. Sem live GO.** Snapshot dos16arquivos revisados em
`INDEXA-AUTH-SNAPSHOT-R2.json`; hashes conferidos sem drift antes do commit.
R1: um P1, um P2 e dois P3, todos corrigidos e reavaliados na R2.

- P1: recovery validated reinicia só unidade segura já verificada e comprova
  health antes da leitura; não fica preso após parada no handler de erro.
- P2: a leitura SDK agora inclui `list_joined_groups`, ferramenta existente
  somente leitura que preserva erro HTTP; zero labels não oculta401.
- P3: Deferred depois de arming propaga para recibo/notificação; secure-hold
  só é declarado após publicar/recarregar/conferir wrapper de boot fechado.
- Adicional: mudança do conteúdo atual do token durante a prova é recusada;
  nenhum hash/valor de token é persistido ou restaurado.

Verificação local após correções: `scripts/ci/check.py` completo com Python
3.11.18 no Mac, Go test aprovado e121testes Python (36produto,22manutenção,
37cliente,26auth). actionlint e diff-check aprovados. Dados sintéticos;
nenhuma escrita/restart na VM, push, PR, Actions, envio ou LLM de produto.
Recibos completos R1/R2 copiados byte a byte para reviews central; totais em
`INDEXA-AUTH-USAGE.json`: USD1.1790804, entrada34/saída20098,
cache criado85868/lido450202. EquivalenteAPI, não cobrança adicional inferida.

Próximo passo da coordenadora: PRdraft/ready/CI oficial/merge/release próprio,
bootstrap versionado separado, reconciliar/provisionar arquivo Indexa aprovado,
policy permanente e readiness real fresca; retomar timer e reler success,
401/200/health/SDK pelo caminho normal. [Runbook](INDEXA-AUTH-RUNBOOK.md).

Limites concretos: o diretório conhecido só continha tokens Melancia, que não
podem ser usados. UV_NO_ENV_FILE exige comprovar ausência de dependência de
dotenv nessa entrada. GetJoinedGroups consulta WhatsApp somente leitura; erro
transitório bloqueia a prova. Reboot ainda em arming, antes de publicar wrapper,
pode manter a configuração inicialmente aberta até o próximo poll; secure-hold
é a fronteira com garantia de boot fechado. UID1000/sudo/docker ficam separados.
Não declarar auth resolvida antes de ativação e prova real pela coordenadora.
