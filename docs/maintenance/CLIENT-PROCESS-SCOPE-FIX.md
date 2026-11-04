# Escopo da inspeção de processos — correção local

Base main bca156b410432626f341d8ba183cd079bf665811, PR16 integrado. A coordenadora
confirmou CI41748934305 e release41749440740 verdes, canário Linux e stage real
da VM bem-sucedidos; nenhum cutover/policy/readiness/journal de cliente instalado.
O primeiro controller encontrou PermissionError em exe de sd-pam/sshd/gpg-agent,
todos com cmdline legível e SEM referência ao cliente. Esses fatos foram
reportados pela coordenadora; esta frente não executou comandos na VM.

Objetivo: classificar argv antes de readlink/exe/cgroup/outros atributos. Não
excluir nomes de processos ad-hoc e não enfraquecer a cerca de runtimes relevantes.

Mudança: filtro de caminho passa imediatamente após cmdline. Irrelevantes saem
sem consultar exe/cgroup/uid. Havendo evidência positiva de relevância, qualquer
PermissionError bloqueia, mesmo se a identidade não puder ser inspecionada ou
for outro UID. Cmdline desconhecido/inacessível do próprio usuário mantém o
fail-closed anterior. Executáveis relevantes `(deleted)` continuam registrados
e reprovam mapped/quiet; processo relevante fora do cgroup permitido ainda bloqueia.

Cobertura:6regressões sintéticas para os casos acima +19testes existentes de
runtime passaram. Contraprova executada com processes() da base em memória:
o teste do processo irrelevante protegido falhou com o ValueError original.
Não houve repetição da suíte89. Não muda ABI, artefato, pin, dados, launcher, auth, unidade ou política.
O probe avulso central NÃO faz parte deste commit. Revisão proporcional concluída:
Anthropic `claude-opus-5-5` efetivo, success/ok, CODEGO semP1/P2/P3. Implementação
OpenAI/Codex. Recibo completo CLIENT-PROCESS-SCOPE-OPUS55.json, cópia idêntica
central em reviews/hermes-mcp-client-process-scope-20261104-opus55.json.
Custo equivalente USD0.107727: entrada8, saída2149, cachecriado7410/lido27175.
Sem push/Actions/instalação/VM por esta frente.

A coordenadora deve publicar pelo PR/CI e fazer novo bootstrap VERSIONADO no SHA
integrado; não copiar código para control-releases existente, não restaurar o
payload já íntegro, não adiantar o checkout vivo e não executar --poll manual.
