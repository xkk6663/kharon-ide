$root = "C:\Users\xiao1\Desktop\esp32\zephyr-learn\zephyr_k"
$tmp  = "$root\.tmp-tfpsa.zip"
$python = "C:\Users\xiao1\Desktop\esp32\zephyr-learn\.venv\Scripts\python.exe"
$gh = "github.com/zephyrproject-rtos/tf-psa-crypto/archive/765af96a20a2b408474dcd950372ba9af4d26b62.zip"
$mirrors = @("https://gh-proxy.com/https://$gh", "https://ghfast.top/https://$gh", "https://github.moeyy.xyz/https://$gh", "https://gh.llkk.cc/https://$gh", "https://github.com/zephyrproject-rtos/tf-psa-crypto/archive/765af96a20a2b408474dcd950372ba9af4d26b62.zip")
$curl = "$env:SystemRoot\System32\curl.exe"

$ok = $false
for ($i = 0; $i -lt 12 -and -not $ok; $i++) {
  $m = $mirrors[$i % $mirrors.Count]
  Write-Output ("[try " + ($i+1) + "] " + $m)
  & $curl -s -L --retry 4 --retry-all-errors --retry-delay 2 --max-time 420 -C - -o $tmp $m
  $r = & $python -c "import zipfile,sys; z=zipfile.ZipFile(r'$tmp'); print('OK entries=',len(z.namelist()),'bad=',z.testzip())" 2>$null
  if ($LASTEXITCODE -eq 0 -and $r) { Write-Output $r; $ok = $true }
  else { Write-Output ("  incomplete: " + [math]::Round((Get-Item -LiteralPath $tmp).Length/1MB,2) + " MB") }
}
Write-Output ("RESULT ok=" + $ok)
