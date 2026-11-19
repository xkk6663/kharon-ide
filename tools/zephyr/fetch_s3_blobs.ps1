$ErrorActionPreference = "Continue"
$lib = "C:\Users\xiao1\Desktop\esp32\zephyr-learn\zephyr_k\modules\hal\espressif\lib\esp32s3"
$curl = "$env:SystemRoot\System32\curl.exe"
if (-not (Test-Path -LiteralPath $lib)) { New-Item -ItemType Directory -Path $lib -Force | Out-Null }
$mirrors = @("https://gh-proxy.com/", "https://ghfast.top/", "")

$blobs = @(
  @{ n="libcore.a";     u="https://github.com/espressif/esp32-wifi-lib/raw/cde32e0a504ec4083261156f807babf702967bb2/esp32s3/libcore.a";     h="64e8ab87fbecc43fb07e6059f635fbeb8154f3f13accf56af4b1c3f47160cd70" },
  @{ n="libespnow.a";   u="https://github.com/espressif/esp32-wifi-lib/raw/cde32e0a504ec4083261156f807babf702967bb2/esp32s3/libespnow.a";   h="5374e802eb92f110c0bc32897f2e2275bec9950250249f229ce5957c2ea112ab" },
  @{ n="libnet80211.a"; u="https://github.com/espressif/esp32-wifi-lib/raw/cde32e0a504ec4083261156f807babf702967bb2/esp32s3/libnet80211.a"; h="0a9259152e71171399a9189f5995c3da27243e7d5dddf7b75403e88e0d0abf3d" },
  @{ n="libpp.a";       u="https://github.com/espressif/esp32-wifi-lib/raw/cde32e0a504ec4083261156f807babf702967bb2/esp32s3/libpp.a";       h="152d3b09e9ff3dc2bf6ec812ad31e65be19dfb77b384388f1a63450de4069a69" },
  @{ n="libmesh.a";     u="https://github.com/espressif/esp32-wifi-lib/raw/cde32e0a504ec4083261156f807babf702967bb2/esp32s3/libmesh.a";     h="d71e16bfabcb69f0254ccd124f405c91c60a2868b9616f889fc67d8436dc6383" },
  @{ n="libbtdm_app.a"; u="https://github.com/espressif/esp32c3-bt-lib/raw/0a08c4b32f3666003080b662a1a61794da24ff0f/esp32s3/libbtdm_app.a"; h="513488f088f7806c5a815580fd00e189265962a8824091b1e60618b7f345d0c1" },
  @{ n="libbtbb.a";     u="https://github.com/espressif/esp-phy-lib/raw/e37ecdc9c1e93fefda5b1a99f09bd979046d0720/esp32s3/libbtbb.a";       h="4d60de87cad0fa9519d68e0e40778a68dc61af040853fd0b5c45efa2a010ee94" },
  @{ n="libphy.a";      u="https://github.com/espressif/esp-phy-lib/raw/e37ecdc9c1e93fefda5b1a99f09bd979046d0720/esp32s3/libphy.a";        h="038ac2bc37dc297e295811bc095d2b360a49aa0dddaa51ccb4b60ce2d1b2b73f" },
  @{ n="libcoexist.a";  u="https://github.com/espressif/esp-coex-lib/raw/3c490f8059e0a7eeedda30aaa1e4b3afad06f632/esp32s3/libcoexist.a";  h="81ef0c4b60fb153faf7e68dc5b63a8c66a578d82986033751e9961fa3abfb79d" }
)

foreach ($b in $blobs) {
  $dst = Join-Path $lib $b.n
  $ok = $false
  if (Test-Path -LiteralPath $dst) {
    $h = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash.ToLower()
    if ($h -eq $b.h) { Write-Output ("SKIP " + $b.n + " (already ok)"); $ok = $true }
  }
  if (-not $ok) {
    foreach ($m in $mirrors) {
      $url = $m + $b.u
      & $curl -s -L --retry 3 --retry-delay 2 --max-time 300 -o $dst $url
      if (Test-Path -LiteralPath $dst) {
        $h = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash.ToLower()
        if ($h -eq $b.h) { Write-Output ("OK   " + $b.n + "  " + [math]::Round((Get-Item -LiteralPath $dst).Length/1KB) + " KB  via " + $(if ($m) {$m} else {"direct"})); $ok = $true; break }
        else { Write-Output ("BAD  " + $b.n + " sha=" + $h.Substring(0,12) + " via " + $(if ($m) {$m} else {"direct"})) }
      }
    }
  }
  if (-not $ok) { Write-Output ("FAIL " + $b.n) }
}
Write-Output "ALLDONE"
Get-ChildItem -LiteralPath $lib -File | ForEach-Object { Write-Output ("  " + $_.Name + "  " + [math]::Round($_.Length/1KB) + " KB") }
