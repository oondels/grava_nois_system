# Origem por câmera e quadra

O contrato cruzado está em `grava_nois_api/docs/specs/backend/CAMERA_VENUE_ASSIGNMENTS.md`
no workspace. Dispositivos fixos podem usar `cameras[].venueId` para quadras do mesmo
cliente, após cadastro administrativo na API. Não usar esse campo em rental.

A configuração legada sem venueId mantém a quadra principal. Quando presente,
venueId deve ser UUID; é levado a CaptureConfig e preservado no enqueue imediato e
na admissão diferida. Sidecar/manifesto guarda camera_id, capture_venue_id e
camera_assignment_enabled. Não reconstruir esses campos pela configuração atual.

Worker e retry escolhem a quadra do snapshot; API client assina a rota dessa quadra.
Gateway diferido mantém identidade principal para detectar troca de equipamento,
mas envia capture_venue_id como venue_id da ingestão e camera_id conforme as flags
de origem descritas abaixo.
Importação para v3 preserva os campos do sidecar; jobs antigos usam rota legada.
Eventos v2 de jobs mapeados carregam capture_venue_id; envelope, assinatura, ACK,
status agregado e manutenção mantêm a quadra principal.

Trocas exigem parar captura e confirmar configuração antes de alterar o cadastro;
retomar só com configuração da nova quadra. Capturas preservadas antes do corte
continuam entregáveis. Não voltar a binário antigo com pendências mapeadas, mesmo
em manifestos v3: versões antigas ignoram o snapshot da quadra.

Aplicar migration/API primeiro, depois edge e configuração. Testes isolados cobrem
snapshot imediato, reinício diferido e configuração de duas câmeras; validar também
o piloto físico antes de produção. Não há migration ou operação física nesta entrega.

## Identificação obrigatória em novos clipes

Novas capturas do edge persistem `camera_metadata_enabled=true`, separadamente de
`camera_assignment_enabled`. Worker, retry e gateway enviam a câmera quando qualquer
uma dessas flags estiver ativa. Sem ambas, a fila antiga conserva o payload legado,
mesmo que contenha camera_id local; não enriquecer reservas remotas antigas.
Importação para v3 e armazenamento rental offline preservam o snapshot e as flags.
Fixed sem cameras[].venueId envia a quadra principal preservada na captura e exige
vínculo válido também nessa quadra. Não cadastrar vínculos automaticamente.

Rental aceita camera_id opcional (string não vazia, até 120 caracteres), sem vínculo
com quadra, e persiste camera_id e capture_device_id autenticado. Deduplicação com
origem divergente retorna 409 capture_origin_conflict, inclusive câmera ausente em
uma reserva identificada. Clientes antigos continuam aceitos sem câmera; não há
backfill. HMAC, contrato temporal, acesso e retenção permanecem obrigatórios.

Disponibilizar API antes do edge e cadastrar vínculos fixed antes de ativar novas
capturas. Colunas existentes são reutilizadas; nenhuma migration foi executada.
Validação isolada com doubles não comprova persistência em produção ou hardware.

Validação em 2026-11-04: 102 testes direcionados aprovados: worker, retry, gateway diferido, recuperação, rental, persistência e caracterização. Testes isolados, sem serviços reais.
