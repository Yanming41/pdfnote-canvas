# Reads the PDF annotation prompt from stdin and returns a concise Claude answer.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$prompt = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($prompt)) {
  Write-Output '[pdfnote-ai] empty prompt'
  exit 0
}
$prompt | claude -p --no-session-persistence --permission-mode dontAsk
exit $LASTEXITCODE
