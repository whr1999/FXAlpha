param(
    [ValidateSet("check", "ensure")]
    [string] $Mode = "check",
    [string[]] $TushareIps = @("8.140.225.26", "60.205.198.20"),
    [switch] $Json
)

$ErrorActionPreference = "Stop"
$GuardVersion = "2026-06-15-0735-route-exe-ps51-compatible"
$ProxyInterfacePattern = "(?i)(flclash|clash|mihomo|tun|tap|wintun|wireguard|tailscale|zerotier|vpn)"
$TushareIps = @(
    $TushareIps |
        ForEach-Object { [string] $_ -split "," } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)

function Get-RouteInterfaceAlias {
    param($Route)
    if ($Route.InterfaceAlias) {
        return [string] $Route.InterfaceAlias
    }
    if ($null -ne $Route.InterfaceIndex) {
        try {
            $adapter = Get-NetAdapter -InterfaceIndex $Route.InterfaceIndex -ErrorAction Stop
            return [string] $adapter.InterfaceAlias
        } catch {
            return ""
        }
    }
    return ""
}

function Test-ProxyLikeRoute {
    param($Route)
    if ($null -eq $Route) {
        return $true
    }
    $alias = Get-RouteInterfaceAlias $Route
    $nextHop = [string] $Route.NextHop
    $ipAddress = [string] $Route.IPAddress
    if ($nextHop.StartsWith("198.18.") -or $ipAddress.StartsWith("198.18.")) {
        return $true
    }
    return $alias -match $ProxyInterfacePattern
}

function Get-BestRouteForIp {
    param([string] $Ip)
    $routes = @(
        Find-NetRoute -RemoteIPAddress $Ip -ErrorAction Stop |
            Where-Object { $_.DestinationPrefix -and $_.DestinationPrefix -ne "" } |
            Sort-Object `
                @{Expression = { if ($_.DestinationPrefix -eq "$Ip/32") { 0 } else { 1 } }}, `
                @{Expression = { if ($null -eq $_.RouteMetric) { [int]::MaxValue } else { [int] $_.RouteMetric } }}, `
                @{Expression = { if ($null -eq $_.InterfaceMetric) { [int]::MaxValue } else { [int] $_.InterfaceMetric } }}
    )
    if ($routes.Count -eq 0) {
        return $null
    }
    return $routes[0]
}

function Get-DirectDefaultRoute {
    $routes = @(
        Get-NetRoute -DestinationPrefix "0.0.0.0/0" -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" -and -not (Test-ProxyLikeRoute $_) } |
            Sort-Object `
                @{Expression = { if ($null -eq $_.RouteMetric) { [int]::MaxValue } else { [int] $_.RouteMetric } }}, `
                @{Expression = { if ($null -eq $_.InterfaceMetric) { [int]::MaxValue } else { [int] $_.InterfaceMetric } }}
    )
    if ($routes.Count -eq 0) {
        return $null
    }
    return $routes[0]
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-RouteExe {
    param([string[]] $Arguments)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = "route.exe"
    $psi.Arguments = ($Arguments | ForEach-Object {
        $arg = [string] $_
        if ($arg -match '[\s"]') {
            '"' + $arg.Replace('"', '\"') + '"'
        } else {
            $arg
        }
    }) -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    $output = ($stdout + "`n" + $stderr).Trim()
    return [ordered] @{
        exit_code = $proc.ExitCode
        output = $output
    }
}

function Ensure-HostRoute {
    param(
        [string] $Ip,
        $DirectRoute
    )
    $add = Invoke-RouteExe @(
        "-p",
        "add",
        $Ip,
        "mask",
        "255.255.255.255",
        [string] $DirectRoute.NextHop,
        "metric",
        "1",
        "if",
        [string] $DirectRoute.InterfaceIndex
    )
    if ($add.exit_code -ne 0) {
        $change = Invoke-RouteExe @(
            "change",
            $Ip,
            "mask",
            "255.255.255.255",
            [string] $DirectRoute.NextHop,
            "metric",
            "1",
            "if",
            [string] $DirectRoute.InterfaceIndex
        )
        if ($change.exit_code -ne 0) {
            throw "route.exe persistent add/change failed for ${Ip}: add=[$($add.output)] change=[$($change.output)]"
        }
    }
}

$report = [ordered] @{
    status = "unknown"
    mode = $Mode
    route_guard_version = $GuardVersion
    checked_at = (Get-Date).ToString("s")
    direct_default_route = $null
    hosts = [ordered] @{}
    issues = @()
    warnings = @()
}

try {
    $directRoute = Get-DirectDefaultRoute
    if ($null -ne $directRoute) {
        $report.direct_default_route = [ordered] @{
            interface_alias = Get-RouteInterfaceAlias $directRoute
            interface_index = $directRoute.InterfaceIndex
            next_hop = [string] $directRoute.NextHop
            route_metric = $directRoute.RouteMetric
            interface_metric = $directRoute.InterfaceMetric
        }
    } else {
        $report.issues += "host_direct_default_route_missing"
    }

    foreach ($ip in $TushareIps) {
        $route = Get-BestRouteForIp $ip
        $usesProxy = Test-ProxyLikeRoute $route
        $hostReport = [ordered] @{
            ip = $ip
            route_is_direct = ($null -ne $route -and -not $usesProxy)
            route_uses_proxy_tun = $usesProxy
            destination_prefix = if ($null -ne $route) { [string] $route.DestinationPrefix } else { $null }
            interface_alias = if ($null -ne $route) { Get-RouteInterfaceAlias $route } else { $null }
            interface_index = if ($null -ne $route) { $route.InterfaceIndex } else { $null }
            next_hop = if ($null -ne $route) { [string] $route.NextHop } else { $null }
            route_metric = if ($null -ne $route) { $route.RouteMetric } else { $null }
            interface_metric = if ($null -ne $route) { $route.InterfaceMetric } else { $null }
        }
        if ($hostReport.route_uses_proxy_tun) {
            $report.issues += "host_tushare_route_uses_proxy_tun:$ip"
        }
        $report.hosts[$ip] = $hostReport
    }

    if ($Mode -eq "ensure" -and $report.issues.Count -gt 0) {
        if ($null -eq $directRoute) {
            throw "Cannot ensure Tushare routes because no non-proxy IPv4 default route is available."
        }
        if (-not (Test-Admin)) {
            $report.issues += "host_tushare_route_admin_required"
            throw "Administrator privileges are required to add persistent Tushare host routes."
        }
        foreach ($ip in $TushareIps) {
            Ensure-HostRoute -Ip $ip -DirectRoute $directRoute
        }
        $report.issues = @()
        foreach ($ip in $TushareIps) {
            $route = Get-BestRouteForIp $ip
            $usesProxy = Test-ProxyLikeRoute $route
            $report.hosts[$ip] = [ordered] @{
                ip = $ip
                route_is_direct = ($null -ne $route -and -not $usesProxy)
                route_uses_proxy_tun = $usesProxy
                destination_prefix = if ($null -ne $route) { [string] $route.DestinationPrefix } else { $null }
                interface_alias = if ($null -ne $route) { Get-RouteInterfaceAlias $route } else { $null }
                interface_index = if ($null -ne $route) { $route.InterfaceIndex } else { $null }
                next_hop = if ($null -ne $route) { [string] $route.NextHop } else { $null }
                route_metric = if ($null -ne $route) { $route.RouteMetric } else { $null }
                interface_metric = if ($null -ne $route) { $route.InterfaceMetric } else { $null }
            }
            if ($usesProxy) {
                $report.issues += "host_tushare_route_uses_proxy_tun:$ip"
            }
        }
    }

    $report.status = if ($report.issues.Count -eq 0) { "ok" } else { "failed" }
} catch {
    if ($report.issues.Count -eq 0) {
        $report.issues += "host_tushare_route_guard_failed"
    }
    $report.warnings += [string] $_.Exception.Message
    $report.status = "failed"
}

if ($Json) {
    $report | ConvertTo-Json -Depth 8
} else {
    $report | Format-List
}

if ($report.status -eq "ok") {
    exit 0
}
exit 1
