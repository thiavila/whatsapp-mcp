# Indexa8080: primeira ativação pelo pipeline oficial

Implementação local, sem ativação nesta frente. Auth8080 continua aberta até a
coordenadora executar e reler o resultado real. UID1000/sudo/docker permanecem
um achado separado. Este lote não substitui Python, Go, launcher Hermes,
turnowner, sessões, stores ou o repositório vivo.

## Contratos persistidos

- Workflow `indexa-auth-release.yml`: somente main, merged PR + CI oficial;
  artefato `indexa-auth-release` com `auth.json` e `manifest.json`. Payload
  tipado contém enforce/8080/token_ref, sem arquivo nem valor de token; manifesto
  SHA256 canônico do config e hashes dos cinco módulos auth + executor instalado.
- Executor rejeita artefato divergente do controle instalado. Bootstrap é
  versionado, de checkout separado no SHA revisado, como já descrito em
  `CLIENT-ACTIVATION-GATE.md`. Nunca copiar script sobre control-releases vivo.
- `indexa-auth-policy.json` e `indexa-auth-readiness.json`: arquivo regular
  root-owned, nlink1, sem escrita por grupo/outros, máximo64KiB.
- `indexa-auth-stage.json`: metadados para conferência; NÃO é autorização.
- `indexa-auth-pending.json`: phase staged/arming/secure-hold/validated, somente
  hashes, estados de unidades, referências/configuração segura. Nenhum token,
  env completo, banco ou corpo de cliente. Não apagar para destravar fila.
- `indexa-auth-last-success.json`: recibo da validação. O status HTTP200 do
  health sozinho não basta: o probe privado exige connected/logged_in/enforce.

## Pré-requisitos e policy permanente

1. Root sincroniza a branch local, confere diff/revisão e faz um push, PR draft,
   ready depois dos ajustes e CI oficial verde. Só root faz merge/deploy.
2. Manutenção autônoma é ator ativo: usar o lease temporário/retomada existente
   da coordenação antes de merge/bootstrap. Não desabilitar indefinidamente.
   Nenhum poll manual, nem FF/pull no checkout do aplicativo na VM.
3. Root revisa o artefato publicado e instala o controlador completo versionado
   do SHA do merge, pelo bootstrap de infraestrutura já autorizado. Release de
   auth não recompila nem publica Go e não dispara release de cliente.
4. O executor aguarda cliente gerido já ativado, arquivo/árvore verificados,
   configs de todos8perfis conhecidas, processos mapeados e nenhum outro journal.
5. Reconciliar a referência de segredo Indexa existente. No diretório conhecido
   só havia tokens Melancia, que NÃO servem aqui. Se necessário, root provisiona
   previamente arquivo próprio aprovado seguindo o procedimento de segredos.
   Este código nunca gera, copia ou gira credencial. Arquivo regular nlink1,
   dono root ou1000, sem escrita de grupo/qualquer acesso outros, legível por
   thiago; valor ASCII imprimível32..512caracteres. Nenhum symlink no caminho.
   Token diferente por propósito e referência não compartilhada com Melancia.
6. Root cria policy permanente, sem copiar valores do token, em
   `/home/thiago/hermes-maintenance/mcp-deploy/indexa-auth-policy.json`:

```json
{
  "schema": 1,
  "enabled": true,
  "repository": "thiavila/whatsapp-mcp",
  "workflow": ".github/workflows/indexa-auth-release.yml",
  "activation_sha": "<SHA REAL DO MERGE, 40 HEX>",
  "token_ref": "indexa-rest",
  "token_file": "/home/thiago/.config/whatsapp-bridges/<ARQUIVO INDEXA APROVADO>.token",
  "consumers": [{"profile": "indexa-roberto", "server": "whatsapp_indexa"}]
}
```

Placeholders acima são ilustrativos e rejeitados pelo código. O schema é
exato. Consumidores autorizados precisam corresponder ao inventário real, não
uma lista antiga. Só é suportada uma entrada Indexa por arquivo (vários arquivos
são suportados). Arquivo ausente ou entrada adicional inesperada impede corte.

## Stage → prova fresca → execução autônoma

Retomar o timer após lease; sem readiness o executor apenas faz stage/plano.
Ele escreve `indexa-auth-stage.json` com SHA/config/control/policy/unit/client/
binary e inventário dos8perfis. Root relê os metadados e comprova, na VM:

- referência e proveniência da credencial, sem acessar/reutilizar Melancia;
- consumidores externos/configuradores delimitados cercados durante corte;
  scripts manuais conhecidos não devem ser usados durante a transação;
- ambiente efetivo da entrada, sem proxy desviando loopback, sem necessidade
  de dotenv para outros recursos (`UV_NO_ENV_FILE` será ligado só nessa entrada);
- compatibilidade real do perfil com runner gerido e ferramentas somente leitura;
  snapshots/clientes recém-migrados não dispensam conferir drift desta etapa.

Só depois root cria readiness com cópia exata dos campos do stage mais:

```json
{
  "external_consumers_fenced": true,
  "effective_environment_verified": true,
  "token_provenance_indexa": true,
  "profile_tools_compatible": true,
  "checked_at": 0,
  "expires_at": 0
}
```

Os tempos acima são placeholders: usar epoch UTC real, validade máxima1h,
checked_at não futuro. Não preencher booleano sem prova. O arquivo precisa
ter também TODOS os campos exatos do stage, não apenas esse fragmento.

O tick autônomo confere novamente os inputs, lê labels/health por REST e faz
initialize/list_tools/list_joined_groups/list_labels pelo runner real proposto
antes de enforce. São duas ferramentas existentes somente leitura: a primeira
preserva erro HTTP e evita que um401 vire lista vazia aparentemente válida.
Metadados de grupos ficam só no processo privado, sem exportação ou persistência.
O loader do produto deve resolver em memória o header do arquivo aprovado.
Pausa timers/gateway/dashboard sob guarda de trabalho nativa; journal staged
antecede qualquer stop. Após conferir tudo parado e inalterado, persiste
arming irreversível, para só a bridge Indexa, publica dropin próprio atomicamente,
confere ExecStart e só então grava secure-hold (reboot já falha fechado),
aplica cinco envs Indexa, inicia mesmo Go e faz 401sem/200com +health conectado
e SDK. Só então phase validated, retoma os estados anteriores e grava success.
Nenhum corpo/token sai: probes imprimem somente booleanos/status/contagens.

Root deve reler `indexa-auth-last-success.json`, ausência de pending, ExecStart
efetivo e PIDs/health das outras bridges. Repetir prova negativa/positiva e
SDK pelo caminho normal efetivo, usando teste somente leitura, sem LLM/envio.
Isso, CI e revisão fecham a ativação; teste sintético local não fecha produção.

## Recuperação e limite do rollback

Antes de secure-hold: recusa nativa ou crash retoma estados anteriores, remove
apenas journal staged; não há modificação de auth a desfazer.

Depois de arming: nunca remover dropin para voltar a authoff. Recuperação
offline sob mesmo lock para Indexa primeiro mesmo se token/policy sumirem,
valida referências e runtime, reaplica envs por campo e tenta completar enforce
com o conteúdo ATUAL do arquivo aprovado. Não restaura bytes de credencial,
DB, sessões, .env completo ou comando antigo. Outras filas aguardam esse journal.
Se a falha ocorre após validação e antes de concluir retomada, recovery inicia
Indexa segura se necessário e repete health/leituras antes de retomar; não para
um novo turno já começado por rotina normal. Antes da publicação atômica do
wrapper, arming ainda é uma transição da configuração inicialmente aberta;
somente secure-hold afirma garantia de boot fechado.

Conflito com terceiro, binário/policy diferentes ou health/SDK falho mantém
Indexa parada e consumidores cercados. Timer continua tentando e notifica uma
vez por candidato. Root deve reconciliar o valor/arquivo ATUAL aprovado ou
reparar só os campos/refs conflitantes após inspecionar o diff. Não há rollback
automático para a primeira configuração sem token: ela era insegura. Esta entrega
não gerencia futuras trocas de policy; a referência/hash permanente faz wrapper
falhar fechado se alguém a alterar sem reconciliação. Não apagar control versionado
referido pelo ExecStart. AdminUID1000 com sudo pode contornar isso deliberadamente;
separação de identidade fica em lote próprio.

## Verificações locais

26 testes sintéticos auth cobrem YAML/CRLF/bytes opacos, credencial inline,
confusão de referências, alias/duplicata, apply parcial, conflito estrangeiro,
wrapper fail-closed, journal/crash/recuperação offline, ordem config/enforce,
401/200 HTTP real em loopback sintético, erro SDK401 com zero labels, recovery
validated com bridge parada, arming antes de boot-guard e health200 desconectado. Regressão
cliente e manutenção são incluídas no mesmo `scripts/ci/check.py` usado pelo CI
oficial e pelo atestado local; a execução do atestado/push pertence à root.
Nenhuma escrita ou reinicialização na VM por esta frente.
