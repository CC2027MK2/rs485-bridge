# RS485 Network Bridge (PowerShell, Windows 7 / PS 2.0 compatible)
# v3: self-healing loop, buffered logging, serial reopen on failure
# ASCII only - do not add Chinese text (PS 2.0 reads UTF-8 without BOM as ANSI)
# Usage: powershell -ExecutionPolicy Bypass -File C:\rs485_bridge.ps1 -ComPort COM2 -Baud 115200 -TcpPort 5001

param(
    [string]$ComPort = "COM2",
    [int]$Baud = 115200,
    [int]$TcpPort = 5001,
    [int]$IdleTimeoutSec = 30
)

$ErrorActionPreference = "Continue"
$logFile = "C:\rs485_bridge.log"

$script:logWriter = $null
try {
    $script:logWriter = New-Object System.IO.StreamWriter($logFile, $true, [System.Text.Encoding]::UTF8)
    $script:logWriter.AutoFlush = $true
} catch {}

function WriteLog([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $msg
    Write-Host $line
    if ($script:logWriter) { try { $script:logWriter.WriteLine($line) } catch {} }
}

function OpenSerial {
    try {
        $p = New-Object System.IO.Ports.SerialPort
        $p.PortName  = $ComPort
        $p.BaudRate  = $Baud
        $p.Parity    = [System.IO.Ports.Parity]::None
        $p.DataBits  = 8
        $p.StopBits  = [System.IO.Ports.StopBits]::One
        $p.ReadTimeout  = 200
        $p.WriteTimeout = 500
        $p.Open()
        return $p
    } catch {
        WriteLog ("SERIAL-FAIL  " + $_.Exception.Message)
        return $null
    }
}

function NewListener {
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $TcpPort)
        $l.Start()
        WriteLog "LISTEN  ok on 0.0.0.0:$TcpPort"
        return $l
    } catch {
        WriteLog ("LISTEN-FAIL  " + $_.Exception.Message)
        return $null
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  RS485 Network Bridge  (PowerShell v3)"
Write-Host "============================================================"
WriteLog "START v3  COM=$ComPort BAUD=$Baud TCP=$TcpPort idle=$IdleTimeoutSec"

$sp = OpenSerial
if ($sp -eq $null) {
    Write-Host "  Cannot open $ComPort - exiting" -ForegroundColor Red
    Write-Host "  Available ports:"
    [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { Write-Host ("    " + $_) }
    exit 1
}
WriteLog "SERIAL  $ComPort @ $Baud 8N1 opened"
Write-Host "  Serial : $ComPort @ $Baud 8N1"

$listener = NewListener
if ($listener -eq $null) { $sp.Close(); exit 1 }
Write-Host "  TCP    : 0.0.0.0:$TcpPort"
Write-Host "------------------------------------------------------------"
Write-Host "  Ready. Ctrl+C to stop."
Write-Host "============================================================"
Write-Host ""

$buf = New-Object byte[] 4096
$SelectRead = [System.Net.Sockets.SelectMode]::SelectRead
$clientCount = 0
$client = $null

while ($true) {
    try {
        $client = $null
        while ($client -eq $null) {
            try {
                if ($listener.Pending()) {
                    $client = $listener.AcceptTcpClient()
                } else {
                    Start-Sleep -Milliseconds 100
                }
            } catch {
                WriteLog ("PENDING-ERR  " + $_.Exception.Message + "  -> rebuild listener")
                try { $listener.Stop() } catch {}
                Start-Sleep -Milliseconds 500
                $listener = NewListener
                if ($listener -eq $null) { Start-Sleep -Seconds 2 }
            }
        }

        $clientCount++
        $client.NoDelay = $true
        $remote = "?"
        try { $remote = $client.Client.RemoteEndPoint.ToString() } catch {}
        WriteLog "CONN#$clientCount  $remote"
        $stream = $client.GetStream()
        $lastActive = Get-Date

        while ($true) {
            if (-not $sp.IsOpen) {
                WriteLog "SERIAL  closed unexpectedly, reopening"
                $sp = OpenSerial
                if ($sp -eq $null) { Start-Sleep -Seconds 1; continue }
            }

            $peerGone = $false
            try {
                if ($client.Client.Poll(0, $SelectRead) -and ($client.Client.Available -eq 0)) {
                    $peerGone = $true
                }
            } catch { $peerGone = $true }
            if ($peerGone) { WriteLog "CLOSE   peer gone"; break }

            if ($stream.DataAvailable) {
                $n = $stream.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $sp.Write($buf, 0, $n)
                $hex = ($buf[0..($n-1)] | ForEach-Object { $_.ToString("X2") }) -join " "
                WriteLog "TX($n)  $hex"
                $lastActive = Get-Date
            }

            if ($sp.BytesToRead -gt 0) {
                $m = $sp.BytesToRead
                $rb = New-Object byte[] $m
                $sp.Read($rb, 0, $m) | Out-Null
                $stream.Write($rb, 0, $m)
                $hex2 = ($rb | ForEach-Object { $_.ToString("X2") }) -join " "
                WriteLog "RX($m)  $hex2"
                $lastActive = Get-Date
            }

            if (((Get-Date) - $lastActive).TotalSeconds -gt $IdleTimeoutSec) {
                WriteLog "CLOSE   idle $IdleTimeoutSec s"
                break
            }

            Start-Sleep -Milliseconds 5
        }

        try { $client.Close() } catch {}
        WriteLog "DISC#$clientCount"
        Write-Host ""
    }
    catch {
        WriteLog ("MAIN-ERR  " + $_.Exception.Message)
        Start-Sleep -Milliseconds 800
        try { if ($client -ne $null) { $client.Close() } } catch {}
        if (($sp -eq $null) -or (-not $sp.IsOpen)) { $sp = OpenSerial }
        if ($listener -eq $null) { $listener = NewListener }
    }
}
