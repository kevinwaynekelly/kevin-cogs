<?php
/** Bounded Unraid observations and typed native libvirt operations. */
declare(strict_types=1);

function ariaDiagBin(string $name): string {
    $paths = [
        'df' => '/bin/df', 'getent' => '/usr/bin/getent', 'curl' => '/usr/bin/curl',
        'openssl' => '/usr/bin/openssl', 'virsh' => '/usr/bin/virsh',
        'lsblk' => '/bin/lsblk', 'smartctl' => '/usr/sbin/smartctl',
        'lspci' => '/sbin/lspci', 'lsusb' => '/usr/bin/lsusb',
        'nvidia-smi' => '/usr/bin/nvidia-smi', 'trivy' => '/usr/local/bin/trivy',
        'git' => '/usr/bin/git',
    ];
    if (!isset($paths[$name])) ariaFail('invalid request');
    $path = ariaPath('ARIA_DIAG_'.strtoupper(str_replace('-', '_', $name)), $paths[$name]);
    if (ariaTest() && getenv('ARIA_DIAG_'.strtoupper(str_replace('-', '_', $name))) !== false) return $path;
    if (is_executable($path)) return $path;
    foreach (['/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/', '/usr/local/bin/', '/usr/local/sbin/'] as $base) {
        if (is_executable($base.$name)) return $base.$name;
    }
    return $path;
}
function ariaDiagCommand(string $name, array $args, int $seconds = 2, bool $scrub = true): array {
    $path = ariaDiagBin($name);
    if (!is_executable($path)) return ['available' => false, 'dependency' => $name, 'exit_code' => 127, 'timed_out' => false, 'truncated' => false, 'output' => 'Required host executable is unavailable.'];
    $r = ariaRun(array_merge([$path], $args), $seconds);
    $r['available'] = true;
    if ($scrub) $r['output'] = ariaScrub($r['output']);
    return $r;
}
function ariaDiagFields(array $value, array $keys): array { return array_intersect_key($value, array_flip($keys)); }
function ariaDiagFile(string $path, int $limit = 65536): string {
    if (!is_file($path) || is_link($path)) return '';
    $f = @fopen($path, 'rb');
    if ($f === false) return '';
    $data = stream_get_contents($f, $limit); fclose($f);
    return $data === false ? '' : $data;
}
function ariaDiagJsonLines(array $r, array $keys, int $limit = 256): array {
    $rows = [];
    foreach (explode("\n", trim($r['output'])) as $line) {
        if ($line === '') continue;
        $row = json_decode($line, true);
        if (!is_array($row)) return ['available' => $r['available'] ?? true, 'exit_code' => $r['exit_code'] ?: 1, 'truncated' => $r['truncated'], 'error' => 'Command did not return complete JSON.'];
        $rows[] = ariaDiagFields($row, $keys);
        if (count($rows) >= $limit) break;
    }
    return ['available' => $r['available'] ?? true, 'exit_code' => $r['exit_code'], 'timed_out' => $r['timed_out'], 'truncated' => $r['truncated'] || count($rows) >= $limit, 'rows' => $rows];
}
function ariaDiagHost(string $value): string {
    if (filter_var($value, FILTER_VALIDATE_IP)) return $value;
    if (strlen($value) > 253 || !preg_match('/^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/D', $value) || strpos($value, '..') !== false) ariaFail('invalid request');
    return $value;
}
function ariaDiagPort($port): int {
    if (!is_int($port) || $port < 1 || $port > 65535) ariaFail('invalid request');
    return $port;
}
function ariaDiagResolve(string $host): array {
    if (filter_var($host, FILTER_VALIDATE_IP)) return ['available' => true, 'exit_code' => 0, 'addresses' => [$host], 'timed_out' => false];
    $r = ariaDiagCommand('getent', ['ahosts', ariaDiagHost($host)], 2);
    $ips = [];
    foreach (explode("\n", $r['output']) as $line) {
        $parts = preg_split('/\s+/', trim($line));
        if (filter_var($parts[0] ?? '', FILTER_VALIDATE_IP)) $ips[$parts[0]] = true;
    }
    unset($r['output']); $r['addresses'] = array_slice(array_keys($ips), 0, 16);
    return $r;
}
function ariaDiagPath(string $path): string {
    if ($path === '' || strpos($path, "\0") !== false || strlen($path) > 4096) ariaFail('invalid request');
    $root = realpath(ariaPath('ARIA_DIAG_MNT', '/mnt'));
    $real = realpath($path);
    if ($root === false || $real === false || ($real !== $root && strpos($real, $root.'/') !== 0)) ariaFail('invalid request');
    return $real;
}
function ariaDiagPermissions(string $path): array {
    $path = ariaDiagPath($path); $s = @stat($path);
    if ($s === false) ariaFail('not found');
    $user = function_exists('posix_getpwuid') ? posix_getpwuid($s['uid']) : false;
    $group = function_exists('posix_getgrgid') ? posix_getgrgid($s['gid']) : false;
    return ['path' => $path, 'type' => is_dir($path) ? 'directory' : 'file', 'uid' => $s['uid'], 'gid' => $s['gid'], 'owner' => $user['name'] ?? null, 'group' => $group['name'] ?? null, 'mode' => sprintf('%04o', $s['mode'] & 07777), 'size_bytes' => $s['size'], 'modified_at' => gmdate('c', $s['mtime']), 'recursive' => false, 'note' => 'POSIX ownership and mode only; SMB/NFS and ACL rules can additionally restrict access.'];
}
function ariaDiagHealth(string $state): array {
    $memory = [];
    foreach (explode("\n", ariaDiagFile(ariaPath('ARIA_DIAG_MEMINFO', '/proc/meminfo'))) as $line) {
        if (preg_match('/^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+) kB/', $line, $m)) $memory[$m[1]] = (int)$m[2] * 1024;
    }
    $jobs = []; $latest = null;
    foreach (array_slice(glob($state.'/jobs/*.json') ?: [], -512) as $path) {
        $j = json_decode(ariaDiagFile($path, 1048576), true);
        if (!is_array($j)) continue;
        $status = $j['status'] ?? 'unknown'; $jobs[$status] = ($jobs[$status] ?? 0) + 1;
        $latest = ariaDiagFields($j, ['job_id', 'action', 'status', 'created_at', 'started_at', 'finished_at', 'error']);
        if (is_string($latest['error'] ?? null)) $latest['error'] = ariaScrub($latest['error'], [], 2048);
    }
    $worker = json_decode(ariaDiagFile($state.'/worker-ready.json'), true);
    $pid = (int)($worker['pid'] ?? 0);
    $revision = ariaDiagCommand('git', ['-C', __DIR__, 'rev-parse', 'HEAD'], 1);
    $sha = $revision['exit_code'] === 0 && preg_match('/^[a-f0-9]{40,64}$/D', trim($revision['output'])) ? trim($revision['output']) : null;
    return ['observed_at' => ariaNow(), 'hostname' => gethostname(), 'kernel' => php_uname('r'), 'unraid_version' => trim(ariaDiagFile(ariaPath('ARIA_DIAG_VERSION', '/etc/unraid-version'), 1024)), 'php_version' => PHP_VERSION, 'source_revision' => $sha, 'uptime_seconds' => (float)explode(' ', trim(ariaDiagFile(ariaPath('ARIA_DIAG_UPTIME', '/proc/uptime'), 128)))[0], 'load_average' => function_exists('sys_getloadavg') ? sys_getloadavg() : null, 'memory_bytes' => $memory, 'worker' => ['pid' => $pid ?: null, 'process_exists' => $pid > 0 && is_dir('/proc/'.$pid), 'ready_at' => $worker['ready_at'] ?? null, 'note' => 'PID existence is an observation, not a completed worker round-trip.'], 'queue_counts' => $jobs, 'latest_job' => $latest, 'dependencies' => ['virsh' => is_executable(ariaDiagBin('virsh')), 'smartctl' => is_executable(ariaDiagBin('smartctl')), 'trivy' => is_executable(ariaDiagBin('trivy'))]];
}
function ariaDiagStorage(): array {
    $r = ariaDiagCommand('df', ['-P', '-k'], 3); $rows = [];
    foreach (array_slice(explode("\n", trim($r['output'])), 1) as $line) {
        if (preg_match('/^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(.+)$/D', $line, $m)) $rows[] = ['filesystem' => $m[1], 'size_bytes' => (int)$m[2] * 1024, 'used_bytes' => (int)$m[3] * 1024, 'available_bytes' => (int)$m[4] * 1024, 'used_percent' => (int)$m[5], 'mountpoint' => $m[6]];
    }
    if (!$rows && $r['exit_code'] === 0) $r['exit_code'] = 1;
    $mounts = [];
    foreach (explode("\n", ariaDiagFile(ariaPath('ARIA_DIAG_MOUNTINFO', '/proc/self/mountinfo'))) as $line) {
        $parts = explode(' - ', $line, 2); if (count($parts) !== 2) continue;
        $left = explode(' ', $parts[0]); $right = explode(' ', $parts[1]);
        $mounts[] = ['mountpoint' => $left[4] ?? '', 'filesystem_type' => $right[0] ?? '', 'source' => $right[1] ?? '', 'read_only' => in_array('ro', explode(',', $left[5] ?? ''), true)];
        if (count($mounts) >= 256) break;
    }
    return ['observed_at' => ariaNow(), 'exit_code' => $r['exit_code'], 'timed_out' => $r['timed_out'], 'filesystems' => array_slice($rows, 0, 256), 'mounts' => $mounts, 'truncated' => $r['truncated'] || count($rows) > 256 || count($mounts) >= 256];
}
function ariaDiagShares(): array {
    $root = ariaPath('ARIA_DIAG_SHARES', '/boot/config/shares'); $files = glob($root.'/*.cfg') ?: []; $rows = [];
    $allowed = ['shareComment', 'shareAllocator', 'shareSplitLevel', 'shareFloor', 'shareInclude', 'shareExclude', 'shareUseCache', 'shareCachePool', 'shareCachePool2', 'shareMoverAction', 'shareExport', 'shareSecurity', 'shareReadList', 'shareWriteList', 'shareExportNFS', 'shareSecurityNFS'];
    $truncated = count($files) > 256;
    foreach (array_slice($files, 0, 256) as $file) {
        if (is_link($file)) continue;
        $cfg = @parse_ini_string(ariaDiagFile($file, 16384), false, INI_SCANNER_RAW);
        $rows[] = ['name' => basename($file, '.cfg'), 'settings' => is_array($cfg) ? ariaDiagFields($cfg, $allowed) : [], 'valid' => is_array($cfg)];
        if (strlen(ariaJson($rows)) > 524288) { array_pop($rows); $truncated = true; break; }
    }
    return ['available' => is_dir($root), 'shares' => $rows, 'truncated' => $truncated];
}
function ariaDiagContainer(string $name): array {
    $name = ariaName($name, 'container'); $r = ariaDocker(['inspect', '--type', 'container', $name], 2, false);
    $data = json_decode($r['output'], true); $item = $data[0] ?? null;
    if ($r['exit_code'] !== 0 || $r['truncated'] || !is_array($item)) return ['exit_code' => $r['exit_code'] ?: 1, 'timed_out' => $r['timed_out'], 'error' => 'Container inspection failed or exceeded the output limit.'];
    $secrets = [];
    foreach ($item['Config']['Env'] ?? [] as $env) { $p = explode('=', $env, 2); if (isset($p[1])) $secrets[] = $p[1]; }
    $state = ariaDiagFields($item['State'] ?? [], ['Status', 'Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead', 'ExitCode', 'StartedAt', 'FinishedAt']);
    $state['Error'] = ariaScrub($item['State']['Error'] ?? '', $secrets, 2048);
    $mounts = []; $findings = [];
    foreach (array_slice($item['Mounts'] ?? [], 0, 128) as $mount) {
        $row = ariaDiagFields($mount, ['Type', 'Source', 'Destination', 'RW', 'Propagation']);
        $row['source_exists'] = isset($mount['Source']) && file_exists($mount['Source']);
        $mounts[] = $row;
        if (($mount['Type'] ?? '') === 'bind' && !$row['source_exists']) $findings[] = ['code' => 'missing_bind_source', 'path' => $mount['Source']];
    }
    if ($state['OOMKilled'] ?? false) $findings[] = ['code' => 'out_of_memory', 'detail' => 'Docker reports the container was killed for memory exhaustion.'];
    if (($item['RestartCount'] ?? 0) > 3) $findings[] = ['code' => 'repeated_restarts', 'count' => $item['RestartCount']];
    if (($state['ExitCode'] ?? 0) !== 0) $findings[] = ['code' => 'nonzero_exit', 'exit_code' => $state['ExitCode']];
    $logs = ariaDocker(['logs', '--tail', '80', '--timestamps', $name], 2, false);
    return ['name' => $name, 'observed_at' => ariaNow(), 'state' => $state, 'health' => $item['State']['Health']['Status'] ?? null, 'restart_count' => $item['RestartCount'] ?? null, 'restart_policy' => $item['HostConfig']['RestartPolicy'] ?? null, 'memory_limit_bytes' => $item['HostConfig']['Memory'] ?? null, 'network_mode' => $item['HostConfig']['NetworkMode'] ?? null, 'networks' => array_keys($item['NetworkSettings']['Networks'] ?? []), 'mounts' => $mounts, 'findings' => $findings, 'logs' => ['exit_code' => $logs['exit_code'], 'timed_out' => $logs['timed_out'], 'truncated' => $logs['truncated'], 'output' => ariaScrub($logs['output'], $secrets, 16384)]];
}
function ariaDiagResources(string $state, bool $history, int $limit = 10): array {
    $dir = $state.'/diagnostics';
    if (is_link($dir)) ariaFail('internal error');
    if (!is_dir($dir) && !mkdir($dir, 0700)) ariaFail('internal error');
    $path = $dir.'/resources.json';
    if (is_link($path)) ariaFail('internal error');
    if ($limit < 1 || $limit > 20) ariaFail('invalid request');
    try { $lock = ariaLock($state, 'diagnostics-resources', true); }
    catch (Throwable $e) { ariaFail('management busy'); }
    try {
        $samples = json_decode(ariaDiagFile($path, 4194304), true); if (!is_array($samples)) $samples = [];
        if ($history) {
            $selected = array_slice($samples, -$limit);
            while (strlen(ariaJson($selected)) > 786432 && count($selected) > 1) array_shift($selected);
            return ['samples' => $selected, 'retained_samples' => count($samples), 'note' => 'Samples are collected when resource snapshots run; this tool does not install a periodic collector.'];
        }
        $r = ariaDocker(['stats', '--all', '--no-stream', '--format', '{{json .}}'], 3, false);
        $data = ariaDiagJsonLines($r, ['ID', 'Name', 'CPUPerc', 'MemUsage', 'MemPerc', 'NetIO', 'BlockIO', 'PIDs'], 128);
        $sample = ['observed_at' => ariaNow(), 'load_average' => function_exists('sys_getloadavg') ? sys_getloadavg() : null, 'containers' => $data];
        if ($r['exit_code'] === 0 && isset($data['rows']) && !$data['truncated']) {
            $samples[] = $sample; $samples = array_slice($samples, -64);
            while (strlen(ariaJson($samples)) > 4194304 && count($samples) > 1) array_shift($samples);
            ariaAtomic($path, ariaJson($samples));
        }
        return $sample;
    } finally { ariaUnlock($lock); }
}
function ariaDiagHistoryPath(string $state, string $kind): string {
    if (!in_array($kind, ['storage', 'smart', 'monitor'], true)) ariaFail('invalid request');
    $dir = $state.'/diagnostics';
    if (is_link($dir)) ariaFail('internal error');
    if (!is_dir($dir) && !mkdir($dir, 0700)) ariaFail('internal error');
    $path = $dir.'/'.$kind.'-history.json';
    if (is_link($path)) ariaFail('internal error');
    return $path;
}
function ariaDiagRecord(string $state, string $kind, array $sample): void {
    $path = ariaDiagHistoryPath($state, $kind);
    try { $lock = ariaLock($state, 'diagnostics-history-'.$kind, true); }
    catch (Throwable $e) { ariaFail('management busy'); }
    try {
        $rows = json_decode(ariaDiagFile($path, 4194304), true); if (!is_array($rows)) $rows = [];
        $rows[] = $sample; $rows = array_slice($rows, -288);
        while (strlen(ariaJson($rows)) > 4194304 && count($rows) > 1) array_shift($rows);
        ariaAtomic($path, ariaJson($rows));
    } finally { ariaUnlock($lock); }
}
function ariaDiagHistory(string $state, array $a): array {
    $kind = $a['kind'] ?? ''; $limit = $a['limit'] ?? 10;
    if (!is_int($limit) || $limit < 1 || $limit > 50) ariaFail('invalid request');
    $path = ariaDiagHistoryPath($state, $kind);
    $rows = json_decode(ariaDiagFile($path, 4194304), true); if (!is_array($rows)) $rows = [];
    if (isset($a['device'])) {
        if ($kind !== 'smart' || !is_string($a['device']) || !preg_match('~^/dev/(?:sd[a-z]{1,3}|hd[a-z]{1,3}|nvme[0-9]{1,3}n[0-9]{1,3}|mmcblk[0-9]{1,3})$~D', $a['device'])) ariaFail('invalid request');
        $rows = array_values(array_filter($rows, fn($row) => ($row['device'] ?? null) === $a['device']));
    }
    $selected = array_slice($rows, -$limit);
    while (strlen(ariaJson($selected)) > 786432 && count($selected) > 1) array_shift($selected);
    return ['kind' => $kind, 'samples' => $selected, 'retained_matching_samples' => count($rows), 'maximum_retained_samples' => 288, 'maximum_history_bytes' => 4194304, 'note' => $kind === 'smart' ? 'SMART samples are recorded only by explicit SMART jobs. Background monitoring does not run SMART commands.' : 'Storage and monitor samples are collected by the idle monitoring schedule or explicit reads.'];
}
function ariaDiagStorageSample(string $state): array {
    $r = ariaDiagStorage();
    ariaDiagRecord($state, 'storage', ariaDiagFields($r, ['observed_at', 'exit_code', 'timed_out', 'filesystems', 'truncated']));
    return $r;
}
function ariaDiagSmartSample(string $state, string $device): array {
    $r = ariaDiagSmart($device); $data = $r['data'] ?? [];
    $attributes = [];
    foreach ($data['ata_smart_attributes']['table'] ?? [] as $row) {
        if (in_array($row['id'] ?? null, [5, 9, 187, 188, 190, 194, 197, 198, 199, 241, 242], true)) $attributes[] = ariaDiagFields($row, ['id', 'name', 'value', 'worst', 'thresh', 'when_failed', 'raw']);
    }
    ariaDiagRecord($state, 'smart', ['observed_at' => ariaNow(), 'device' => $device, 'exit_code' => $r['exit_code'], 'timed_out' => $r['timed_out'], 'smartctl_exit_status' => $r['smartctl_exit_status'] ?? null, 'health_warning' => $r['health_warning'] ?? null, 'smart_status' => $data['smart_status'] ?? null, 'temperature' => $data['temperature'] ?? null, 'power_on_time' => $data['power_on_time'] ?? null, 'power_cycle_count' => $data['power_cycle_count'] ?? null, 'ata_attributes' => $attributes, 'nvme_health' => $data['nvme_smart_health_information_log'] ?? null]);
    return $r;
}
function ariaDiagnosticsMonitor(string $state): array {
    try { $lock = ariaLock($state, 'diagnostics-monitor', true); }
    catch (Throwable $e) { ariaFail('management busy'); }
    try {
        $storage = ariaDiagStorageSample($state);
        $format = '{"name":{{json .Names}},"state":{{json .State}},"status":{{json .Status}}}';
        $raw = ariaDocker(['ps', '--all', '--format', $format], 2, false);
        $containers = ariaDiagJsonLines($raw, ['name', 'state', 'status'], 256);
        $storageComplete = $storage['exit_code'] === 0 && !$storage['truncated'];
        $containersComplete = $raw['exit_code'] === 0 && isset($containers['rows']) && !$containers['truncated'];
        $dir = dirname(ariaDiagHistoryPath($state, 'monitor')); $path = $dir.'/monitor-alerts.json';
        if (is_link($path)) ariaFail('internal error');
        $previous = json_decode(ariaDiagFile($path, 524288), true); if (!is_array($previous)) $previous = [];
        $candidates = []; $mounts = [];
        foreach ($storage['mounts'] as $mount) $mounts[$mount['mountpoint']] = $mount;
        foreach ($storage['filesystems'] as $row) {
            $mount = $mounts[$row['mountpoint']] ?? [];
            if (($mount['read_only'] ?? false) || in_array($mount['filesystem_type'] ?? '', ['squashfs', 'iso9660'], true) || strpos($row['mountpoint'], '/var/lib/docker/overlay2/') === 0 || $row['used_percent'] < 90) continue;
            $key = 'storage-'.hash('sha256', $row['filesystem']."\0".$row['mountpoint']);
            $candidates[$key] = ['source' => 'storage', 'severity' => $row['used_percent'] >= 95 ? 'critical' : 'warning', 'message' => $row['mountpoint'].' is '.$row['used_percent'].'% full.', 'metadata' => ['mountpoint' => $row['mountpoint'], 'filesystem' => $row['filesystem'], 'used_percent' => $row['used_percent'], 'available_bytes' => $row['available_bytes']]];
        }
        foreach ($containers['rows'] ?? [] as $row) {
            if (!is_string($row['name'] ?? null) || !is_string($row['state'] ?? null) || !is_string($row['status'] ?? null)) continue;
            $problem = strtolower($row['state']) === 'restarting' ? 'restarting' : (preg_match('/\bunhealthy\b/i', $row['status']) ? 'unhealthy' : null);
            if ($problem === null) continue;
            $key = 'container-'.hash('sha256', $row['name']);
            $candidates[$key] = ['source' => 'container', 'severity' => 'warning', 'message' => $row['name'].' is '.$problem.'.', 'metadata' => ['name' => $row['name'], 'state' => $row['state'], 'problem' => $problem]];
        }
        // Failed/truncated observations cannot establish recovery for an older alert.
        foreach ($previous as $key => $alert) if (!isset($candidates[$key]) && (($alert['source'] === 'storage' && !$storageComplete) || ($alert['source'] === 'container' && !$containersComplete))) $candidates[$key] = $alert;
        $next = [];
        foreach (array_slice($candidates, 0, 512, true) as $key => $alert) {
            $old = $previous[$key] ?? null;
            $same = $old !== null && $old['severity'] === $alert['severity'] && ($old['metadata']['problem'] ?? null) === ($alert['metadata']['problem'] ?? null);
            $alert['id'] = $same ? $old['id'] : 'monitor-'.bin2hex(random_bytes(16));
            $alert['notified'] = $same ? ($old['notified'] ?? false) : false;
            $alert['first_observed_at'] = $same ? $old['first_observed_at'] : ariaNow();
            $next[$key] = $alert;
        }
        ariaAtomic($path, ariaJson($next));
        $delivered = 0;
        if (function_exists('ariaAutomationNotice')) foreach ($next as &$alert) {
            if ($alert['notified'] || $delivered >= 32) continue;
            ariaAutomationNotice($state, $alert['id'], 'monitor_'.$alert['severity'], $alert['message'], ['source' => $alert['source'], 'severity' => $alert['severity']] + $alert['metadata']);
            $alert['notified'] = true; $delivered++;
        }
        unset($alert); ariaAtomic($path, ariaJson($next));
        $summary = ['observed_at' => ariaNow(), 'status' => $storageComplete && $containersComplete ? 'succeeded' : ($storageComplete || $containersComplete ? 'partial' : 'failed'), 'storage_complete' => $storageComplete, 'containers_complete' => $containersComplete, 'filesystem_count' => count($storage['filesystems']), 'container_count' => count($containers['rows'] ?? []), 'active_alerts' => count($next), 'critical_alerts' => count(array_filter($next, fn($alert) => $alert['severity'] === 'critical')), 'notifications_created' => $delivered, 'pending_notifications' => count(array_filter($next, fn($alert) => !$alert['notified'])), 'delivery' => 'local inbox only', 'automatic_repairs' => false];
        ariaDiagRecord($state, 'monitor', $summary);
        return $summary;
    } finally { ariaUnlock($lock); }
}
function ariaDiagNetwork(array $a): array {
    $kind = $a['kind'] ?? '';
    if ($kind === 'http') {
        $url = $a['url'] ?? ''; $p = is_string($url) ? parse_url($url) : false;
        if (!is_array($p) || !in_array($p['scheme'] ?? '', ['http', 'https'], true) || isset($p['user']) || isset($p['pass']) || isset($p['fragment']) || strlen($url) > 2048 || preg_match('/[\x00-\x20\x7f]/', $url)) ariaFail('invalid request');
        ariaDiagHost($p['host'] ?? ''); if (isset($p['port'])) ariaDiagPort($p['port']);
        // No response body, headers, cookies, redirects, credentials or caller-supplied curl options.
        $r = ariaDiagCommand('curl', ['--disable', '--silent', '--show-error', '--noproxy', '*', '--proto', '=http,https', '--connect-timeout', '2', '--max-time', '4', '--output', '/dev/null', '--write-out', '{"http_code":%{http_code},"remote_ip":"%{remote_ip}","total_seconds":%{time_total},"tls_verify_result":%{ssl_verify_result}}', '--url', $url], 5);
        $m = [];
        preg_match('/\{"http_code":.*\}$/s', trim($r['output']), $m);
        $metrics = isset($m[0]) ? json_decode($m[0], true) : null;
        return ['kind' => 'http', 'host' => $p['host'], 'exit_code' => $r['exit_code'], 'available' => $r['available'], 'timed_out' => $r['timed_out'], 'metrics' => $metrics, 'error' => $r['exit_code'] === 0 ? null : 'HTTP request failed; inspect exit_code and TLS verification result.'];
    }
    $host = ariaDiagHost($a['host'] ?? '');
    if ($kind === 'dns') return ['kind' => 'dns', 'host' => $host] + ariaDiagResolve($host);
    if (!in_array($kind, ['tcp', 'tls'], true)) ariaFail('invalid request');
    $port = ariaDiagPort($a['port'] ?? ($kind === 'tls' ? 443 : 0));
    $resolved = ariaDiagResolve($host); $ip = $resolved['addresses'][0] ?? null;
    if ($ip === null) return ['kind' => $kind, 'host' => $host, 'port' => $port, 'connected' => false, 'error' => 'DNS resolution failed.', 'resolution' => $resolved];
    $endpoint = (strpos($ip, ':') === false ? $ip : '['.$ip.']').':'.$port;
    if ($kind === 'tcp') {
        $start = microtime(true); $stream = @stream_socket_client('tcp://'.$endpoint, $errno, $error, 2);
        $ok = is_resource($stream); if ($ok) fclose($stream);
        return ['kind' => 'tcp', 'host' => $host, 'address' => $ip, 'port' => $port, 'connected' => $ok, 'elapsed_ms' => round((microtime(true) - $start) * 1000, 2), 'error_number' => $ok ? null : $errno];
    }
    $r = ariaDiagCommand('openssl', ['s_client', '-connect', $endpoint, '-servername', $host, '-showcerts', '-verify_return_error', filter_var($host, FILTER_VALIDATE_IP) ? '-verify_ip' : '-verify_hostname', $host], 4);
    $certificate = null;
    if (function_exists('openssl_x509_parse') && preg_match('/-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----/s', $r['output'], $m)) {
        $cert = @openssl_x509_parse($m[0]);
        if (is_array($cert)) $certificate = ['subject' => $cert['subject'] ?? [], 'issuer' => $cert['issuer'] ?? [], 'valid_from' => isset($cert['validFrom_time_t']) ? gmdate('c', $cert['validFrom_time_t']) : null, 'valid_until' => isset($cert['validTo_time_t']) ? gmdate('c', $cert['validTo_time_t']) : null, 'days_until_expiry' => isset($cert['validTo_time_t']) ? (int)floor(($cert['validTo_time_t'] - time()) / 86400) : null, 'subject_alternative_names' => $cert['extensions']['subjectAltName'] ?? null];
    }
    return ['kind' => 'tls', 'host' => $host, 'port' => $port, 'available' => $r['available'], 'exit_code' => $r['exit_code'], 'timed_out' => $r['timed_out'], 'verified' => $r['exit_code'] === 0 && $certificate !== null, 'certificate' => $certificate];
}
function ariaDiagVMName($name): string {
    if (!is_string($name) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$/D', $name)) ariaFail('invalid request');
    return $name;
}
function ariaDiagVM(array $a, string $operation): array {
    if ($operation === 'list') {
        $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'list', '--all'], 3);
        return ['observed_at' => ariaNow()] + $r;
    }
    $name = ariaDiagVMName($a['name'] ?? null);
    if ($operation === 'inspect') {
        $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'dumpxml', '--domain', $name], 3, false);
        $tree = null;
        if ($r['exit_code'] === 0 && !$r['truncated'] && !preg_match('/<!DOCTYPE|<!ENTITY/i', $r['output'])) {
            libxml_use_internal_errors(true); $tree = simplexml_load_string($r['output'], 'SimpleXMLElement', LIBXML_NONET); libxml_clear_errors();
        }
        if ($tree === null || $tree === false) return ['name' => $name, 'available' => $r['available'], 'exit_code' => $r['exit_code'] ?: 1, 'timed_out' => $r['timed_out'], 'error' => 'VM XML inspection is unavailable or failed.'];
        $disks = []; $interfaces = []; $devices = [];
        foreach ($tree->devices->disk as $d) $disks[] = ['type' => (string)$d['type'], 'device' => (string)$d['device'], 'source' => (string)($d->source['file'] ?? $d->source['dev'] ?? ''), 'target' => (string)$d->target['dev'], 'bus' => (string)$d->target['bus'], 'format' => (string)$d->driver['type'], 'read_only' => isset($d->readonly)];
        foreach ($tree->devices->interface as $i) $interfaces[] = ['type' => (string)$i['type'], 'mac' => (string)$i->mac['address'], 'network' => (string)($i->source['network'] ?? $i->source['bridge'] ?? ''), 'model' => (string)$i->model['type']];
        foreach ($tree->devices->hostdev as $d) {
            $attrs = []; foreach ($d->source->address->attributes() ?? [] as $k => $v) $attrs[$k] = (string)$v;
            $devices[] = ['type' => (string)$d['type'], 'address' => $attrs, 'vendor_id' => (string)$d->source->vendor['id'], 'product_id' => (string)$d->source->product['id']];
        }
        $s = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'domstate', '--domain', $name], 2);
        return ['name' => (string)$tree->name, 'uuid' => (string)$tree->uuid, 'state' => $s['exit_code'] === 0 ? trim($s['output']) : null, 'memory' => ['value' => (int)$tree->memory, 'unit' => (string)$tree->memory['unit']], 'vcpus' => (int)$tree->vcpu, 'autostart' => null, 'disks' => $disks, 'interfaces' => $interfaces, 'host_devices' => $devices, 'note' => 'Secrets and raw domain XML are not returned.'];
    }
    if (!in_array($operation, ['start', 'shutdown', 'reboot', 'suspend', 'resume'], true)) ariaFail('invalid request');
    $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', $operation, '--domain', $name], 30);
    $s = $r['exit_code'] === 0 ? ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'domstate', '--domain', $name], 3) : null;
    return ['name' => $name, 'operation' => $operation, 'status' => $r['timed_out'] ? 'unknown' : ($r['exit_code'] === 0 ? 'succeeded' : 'failed'), 'request_accepted' => $r['exit_code'] === 0, 'observed_state' => $s !== null && $s['exit_code'] === 0 ? trim($s['output']) : null, 'note' => in_array($operation, ['shutdown', 'reboot'], true) ? 'The guest may complete this request asynchronously or ignore it.' : null] + $r;
}
function ariaDiagDisks(): array {
    $r = ariaDiagCommand('lsblk', ['--json', '--bytes', '--output', 'NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,RO,MODEL'], 3);
    $data = json_decode($r['output'], true); unset($r['output']);
    if (!is_array($data) || $r['truncated']) return $r + ['error' => 'Disk enumeration did not return complete JSON.'];
    return $r + ['block_devices' => $data['blockdevices'] ?? []];
}
function ariaDiagDevices(): array {
    $gpu = ariaDiagCommand('nvidia-smi', ['--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu', '--format=csv,noheader,nounits'], 2);
    $pci = ariaDiagCommand('lspci', ['-nn'], 2); $usb = ariaDiagCommand('lsusb', [], 2);
    return ['observed_at' => ariaNow(), 'nvidia_gpus' => $gpu, 'pci' => $pci, 'usb' => $usb, 'note' => 'Inventory only. Inspect container device mappings and VM host devices to determine assignments.'];
}
function ariaDiagAssignments(): array {
    $ids = ariaDocker(['ps', '--all', '--quiet', '--no-trunc'], 1, false);
    $names = array_values(array_filter(explode("\n", trim($ids['output']))));
    if ($ids['exit_code'] !== 0 || $ids['truncated'] || count($names) > 256) return ['exit_code' => $ids['exit_code'] ?: 1, 'error' => 'Container inventory unavailable or too large.'];
    foreach ($names as $id) if (!preg_match('/^[a-f0-9]{12,64}$/D', $id)) ariaFail('command failed');
    $containers = []; $assignments = [];
    if ($names) {
        $format = '{"name":{{json .Name}},"devices":{{json .HostConfig.Devices}},"device_requests":{{json .HostConfig.DeviceRequests}},"mounts":{{json .Mounts}}}';
        $r = ariaDocker(array_merge(['inspect', '--type', 'container', '--format', $format], $names), 2, false);
        $parsed = ariaDiagJsonLines($r, ['name', 'devices', 'device_requests', 'mounts']);
        if ($r['exit_code'] !== 0 || !isset($parsed['rows']) || $parsed['truncated']) return ['exit_code' => $r['exit_code'] ?: 1, 'error' => 'Device mappings unavailable or too large.'];
        foreach ($parsed['rows'] as $row) {
            $devices = []; $requests = []; $owner = 'container:'.ltrim($row['name'], '/');
            foreach ($row['devices'] ?? [] as $d) {
                $devices[] = ariaDiagFields($d, ['PathOnHost', 'PathInContainer', 'CgroupPermissions']);
                if (isset($d['PathOnHost'])) $assignments[$d['PathOnHost']][] = $owner;
            }
            foreach ($row['device_requests'] ?? [] as $d) $requests[] = ariaDiagFields($d, ['Driver', 'Count', 'DeviceIDs', 'Capabilities']);
            foreach ($row['mounts'] ?? [] as $d) if (strpos($d['Source'] ?? '', '/dev/') === 0) {
                $devices[] = ['PathOnHost' => $d['Source'], 'PathInContainer' => $d['Destination'] ?? '', 'bind_mount' => true];
                $assignments[$d['Source']][] = $owner;
            }
            if ($devices || $requests) $containers[] = ['name' => ltrim($row['name'], '/'), 'devices' => $devices, 'gpu_requests' => $requests];
        }
    }
    $vms = []; $files = glob(ariaPath('ARIA_DIAG_VM_CONFIGS', '/etc/libvirt/qemu').'/*.xml') ?: [];
    foreach (array_slice($files, 0, 128) as $file) {
        $xml = ariaDiagFile($file); if ($xml === '' || preg_match('/<!DOCTYPE|<!ENTITY/i', $xml)) continue;
        libxml_use_internal_errors(true); $tree = simplexml_load_string($xml, 'SimpleXMLElement', LIBXML_NONET); libxml_clear_errors();
        if ($tree === false) continue;
        $devices = [];
        foreach ($tree->devices->hostdev as $d) {
            $attrs = []; foreach ($d->source->address->attributes() ?? [] as $k => $v) $attrs[$k] = (string)$v;
            $devices[] = ['type' => (string)$d['type'], 'address' => $attrs, 'vendor_id' => (string)$d->source->vendor['id'], 'product_id' => (string)$d->source->product['id']];
        }
        if ($devices) $vms[] = ['name' => (string)$tree->name, 'devices' => $devices];
    }
    $shared = [];
    foreach ($assignments as $device => $owners) if (count(array_unique($owners)) > 1) $shared[] = ['device' => $device, 'owners' => array_values(array_unique($owners))];
    return ['containers' => $containers, 'vm_configurations' => $vms, 'shared_container_devices' => $shared, 'truncated' => count($files) > 128, 'note' => 'VM entries describe saved configurations, not current attachment state. Shared GPU/device paths can be intentional; the same device may be represented differently across Docker and libvirt.'];
}
function ariaDiagXML(string $xml, string $root): SimpleXMLElement {
    if (strlen($xml) > ARIA_MAX_XML || preg_match('/<!DOCTYPE|<!ENTITY/i', $xml)) ariaFail('invalid request');
    libxml_use_internal_errors(true); $tree = simplexml_load_string($xml, 'SimpleXMLElement', LIBXML_NONET); libxml_clear_errors();
    if ($tree === false || $tree->getName() !== $root) ariaFail('invalid request');
    return $tree;
}
function ariaDiagVMRaw(string $name): string {
    $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'dumpxml', '--inactive', '--security-info', '--domain', ariaDiagVMName($name)], 3, false);
    if ($r['exit_code'] !== 0 || $r['truncated']) ariaFail($r['timed_out'] ? 'operation timed out' : 'command failed');
    ariaDiagXML($r['output'], 'domain');
    return $r['output'];
}
function ariaDiagVMRedact(string $xml): array {
    $tree = ariaDiagXML($xml, 'domain'); $secrets = [];
    foreach ($tree->xpath('//*') as $i => $node) {
        $sensitive = preg_match('/pass(word|wd)?|token|secret|credential|auth|api.?key/i', $node->getName());
        if ($sensitive && count($node->children()) === 0 && (string)$node !== '') {
            $key = '__ARIA_REDACTED_'.hash('sha256', 'vm-node:'.$i).'__'; $secrets[$key] = (string)$node; $node[0] = $key;
        }
        foreach ($node->attributes() as $key => $value) {
            if ((string)$value === '') continue;
            if ($sensitive || preg_match('/pass(word|wd)?|token|secret|credential|auth|api.?key/i', $key)) {
                $marker = '__ARIA_REDACTED_'.hash('sha256', 'vm-attr:'.$i.':'.$key).'__'; $secrets[$marker] = (string)$value; $node[$key] = $marker;
            }
        }
        // QEMU environment values and opaque command-line arguments can contain credentials.
        if (in_array($node->getName(), ['env', 'arg'], true)) foreach ($node->attributes() as $key => $value) if ($key === 'value' && (string)$value !== '') {
            $marker = '__ARIA_REDACTED_'.hash('sha256', 'vm-opaque:'.$i.':'.$key).'__'; $secrets[$marker] = (string)$value; $node[$key] = $marker;
        }
    }
    return [(string)$tree->asXML(), $secrets];
}
function ariaDiagVMRestore(string $xml, string $old): string {
    $tree = ariaDiagXML($xml, 'domain'); $secrets = ariaDiagVMRedact($old)[1];
    foreach ($tree->xpath('//*') as $node) {
        if (count($node->children()) === 0 && isset($secrets[(string)$node])) $node[0] = $secrets[(string)$node];
        foreach ($node->attributes() as $key => $value) if (isset($secrets[(string)$value])) $node[$key] = $secrets[(string)$value];
    }
    $out = (string)$tree->asXML(); if (strpos($out, '__ARIA_REDACTED_') !== false) ariaFail('invalid request');
    return $out;
}
function ariaDiagVMConfig(string $name): array {
    $xml = ariaDiagVMRaw($name);
    return ['name' => $name, 'sha256' => hash('sha256', $xml), 'xml' => ariaDiagVMRedact($xml)[0], 'note' => 'Inactive configuration. Preserve redaction markers when editing. Saving requires the VM to be shut off.'];
}
function ariaDiagVMOffline(string $name): bool {
    // Query the inactive set, avoiding locale-dependent domstate text.
    $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'list', '--inactive', '--name'], 3);
    return $r['exit_code'] === 0 && !$r['truncated'] && in_array($name, explode("\n", trim($r['output'])), true);
}
function ariaDiagVMExpected(string $name, string $expected): string {
    if (!preg_match('/^[a-f0-9]{64}$/D', $expected)) ariaFail('invalid request');
    $xml = ariaDiagVMRaw($name);
    if (!hash_equals($expected, hash('sha256', $xml))) ariaFail('hash mismatch');
    return $xml;
}
function ariaDiagVMWrite(string $state, array $job): array {
    $a = $job['arguments']; $name = ariaDiagVMName($a['name']);
    $old = ariaDiagVMExpected($name, $a['expected_sha256']); $xml = ariaDiagVMRestore($a['xml'], $old);
    $prior = ariaDiagXML($old, 'domain'); $next = ariaDiagXML($xml, 'domain');
    if ((string)$next->name !== $name || (string)$next->uuid !== (string)$prior->uuid || (string)$prior->uuid === '') ariaFail('invalid request');
    if (!ariaDiagVMOffline($name)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'VM must be shut off before configuration changes.'];
    $id = ariaName($job['job_id'], 'id'); $backup = $state.'/backups/'.$id.'.vm.xml'; $pending = $state.'/backups/'.$id.'.vm-new.xml';
    if (file_exists($backup) || file_exists($pending)) ariaFail('invalid request');
    ariaAtomic($backup, $old); ariaAtomic($pending, $xml);
    try {
        ariaDiagVMExpected($name, $a['expected_sha256']);
        if (!ariaDiagVMOffline($name)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'VM became active; configuration was not changed.'];
        $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'define', '--validate', $pending], 30);
        // Native validation errors can quote secret configuration attributes.
        unset($r['output']);
        return $r + ['name' => $name, 'status' => $r['timed_out'] ? 'unknown' : ($r['exit_code'] === 0 ? 'succeeded' : 'failed'), 'backup_id' => $id.'.vm.xml', 'configuration_saved' => $r['exit_code'] === 0, 'error' => $r['exit_code'] === 0 ? null : 'Native libvirt validation or definition failed. Original configuration backup retained.'];
    } finally { @unlink($pending); }
}
function ariaDiagSnapshotRaw(string $name, string $snapshot): string {
    $r = ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'snapshot-dumpxml', ariaDiagVMName($name), ariaDiagVMName($snapshot)], 3, false);
    if ($r['exit_code'] !== 0 || $r['truncated']) ariaFail($r['timed_out'] ? 'operation timed out' : 'command failed');
    ariaDiagXML($r['output'], 'domainsnapshot'); return $r['output'];
}
function ariaDiagSnapshotRead(string $name, string $snapshot): array {
    $xml = ariaDiagSnapshotRaw($name, $snapshot); $tree = ariaDiagXML($xml, 'domainsnapshot'); $disks = [];
    foreach ($tree->disks->disk as $d) $disks[] = ['name' => (string)$d['name'], 'snapshot' => (string)$d['snapshot']];
    return ['name' => $name, 'snapshot' => (string)$tree->name, 'sha256' => hash('sha256', $xml), 'state' => (string)$tree->state, 'created_at' => (string)$tree->creationTime !== '' ? gmdate('c', (int)$tree->creationTime) : null, 'parent' => (string)$tree->parent->name, 'disks' => $disks];
}
function ariaDiagSnapshotCompatible(SimpleXMLElement $tree): bool {
    $writable = 0;
    foreach ($tree->devices->disk as $d) {
        if ((string)$d['device'] !== 'disk' || isset($d->readonly)) continue;
        $writable++;
        if ((string)$d['type'] !== 'file' || (string)$d->driver['type'] !== 'qcow2' || (string)$d->source['file'] === '' || ((string)$d['snapshot'] !== '' && (string)$d['snapshot'] !== 'internal')) return false;
    }
    return $writable > 0;
}
function ariaDiagSnapshotExecute(string $state, array $job): array {
    $a = $job['arguments']; $name = ariaDiagVMName($a['name']); $snapshot = ariaDiagVMName($a['snapshot']);
    $xml = ariaDiagVMExpected($name, $a['expected_sha256']); $domain = ariaDiagXML($xml, 'domain');
    if (!ariaDiagVMOffline($name)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'VM must be shut off before snapshot creation or reversion.'];
    if (!ariaDiagSnapshotCompatible($domain)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'Only VMs with exclusively file-backed qcow2 writable disks support this snapshot operation.'];
    if ($job['action'] === 'diagnostics_vm_snapshot_create') {
        $args = ['--connect', 'qemu:///system', 'snapshot-create-as', '--domain', $name, '--name', $snapshot, '--description', 'Aria offline internal snapshot', '--atomic', '--validate'];
    } else {
        if (($a['discard_current_disk_changes'] ?? false) !== true) ariaFail('invalid request');
        $saved = ariaDiagSnapshotRaw($name, $snapshot); $tree = ariaDiagXML($saved, 'domainsnapshot');
        if (!preg_match('/^[a-f0-9]{64}$/D', $a['expected_snapshot_sha256'] ?? '') || !hash_equals($a['expected_snapshot_sha256'], hash('sha256', $saved))) ariaFail('hash mismatch');
        if ((string)$tree->state !== 'shutoff' || (string)$tree->domain->uuid !== (string)$domain->uuid || !ariaDiagSnapshotCompatible($tree->domain)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'Reversion requires an offline internal snapshot with matching full VM metadata.'];
        foreach ($tree->disks->disk as $d) if (!in_array((string)$d['snapshot'], ['internal', 'no'], true)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'External disk snapshots are not supported by this revert operation.'];
        $id = ariaName($job['job_id'], 'id'); $backup = $state.'/backups/'.$id.'.vm.xml';
        if (file_exists($backup)) ariaFail('invalid request'); ariaAtomic($backup, $xml);
        $args = ['--connect', 'qemu:///system', 'snapshot-revert', $name, $snapshot];
    }
    // Revalidate as close to the operation as possible. libvirt enforces its own
    // job locking; a concurrent Web UI operation can still make this fail.
    ariaDiagVMExpected($name, $a['expected_sha256']);
    if (!ariaDiagVMOffline($name)) return ['exit_code' => 1, 'timed_out' => false, 'error' => 'VM became active; snapshot operation was not started.'];
    if ($job['action'] === 'diagnostics_vm_snapshot_revert' && !hash_equals($a['expected_snapshot_sha256'], hash('sha256', ariaDiagSnapshotRaw($name, $snapshot)))) ariaFail('hash mismatch');
    $r = ariaDiagCommand('virsh', $args, 300); unset($r['output']);
    return $r + ['name' => $name, 'snapshot' => $snapshot, 'status' => $r['timed_out'] ? 'unknown' : ($r['exit_code'] === 0 ? 'succeeded' : 'failed'), 'completed' => $r['exit_code'] === 0, 'backup_id' => isset($id) ? $id.'.vm.xml' : null, 'error' => $r['exit_code'] === 0 ? null : 'Native snapshot operation failed or timed out. Inspect libvirt state before retrying.'];
}
function ariaDiagSmart(string $device): array {
    if (!preg_match('~^/dev/(?:sd[a-z]{1,3}|hd[a-z]{1,3}|nvme[0-9]{1,3}n[0-9]{1,3}|mmcblk[0-9]{1,3})$~D', $device)) ariaFail('invalid request');
    $r = ariaDiagCommand('smartctl', ['--json', '--all', $device], 30);
    $data = json_decode($r['output'], true); unset($r['output']);
    if (!is_array($data) || $r['truncated']) { $r['exit_code'] = $r['exit_code'] ?: 1; return $r + ['device' => $device, 'error' => 'SMART data unavailable or exceeded the output limit.']; }
    // smartctl uses a bitmask, so failing-health bits still contain useful data.
    $exit = $r['exit_code']; $r['smartctl_exit_status'] = $exit;
    $r['exit_code'] = ($exit & 7) !== 0 ? $exit : 0;
    return $r + ['device' => $device, 'health_warning' => ($exit & 248) !== 0, 'data' => ariaDiagFields($data, ['model_name', 'model_family', 'firmware_version', 'user_capacity', 'rotation_rate', 'power_on_time', 'power_cycle_count', 'temperature', 'smart_status', 'ata_smart_attributes', 'nvme_smart_health_information_log', 'ata_smart_error_log', 'ata_smart_self_test_log'])];
}
function ariaDiagSecurity(string $image): array {
    if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._\/:@-]{0,511}$/D', $image)) ariaFail('invalid request');
    // The fixed template avoids image config, environment, descriptions and other
    // large metadata. Secret scanning is deliberately not requested.
    $template = "{{range .}}{{range .Vulnerabilities}}{{.VulnerabilityID}}\t{{.PkgName}}\t{{.InstalledVersion}}\t{{.FixedVersion}}\t{{.Severity}}{{\"\\n\"}}{{end}}{{end}}";
    $r = ariaDiagCommand('trivy', ['image', '--quiet', '--scanners', 'vuln', '--timeout', '5m', '--format', 'template', '--template', $template, '--exit-code', '0', $image], 330);
    $findings = [];
    if ($r['exit_code'] === 0) foreach (explode("\n", trim($r['output'])) as $line) {
        if ($line === '') continue;
        $parts = explode("\t", $line);
        if (count($parts) !== 5) { $r['truncated'] = true; continue; }
        $findings[] = array_combine(['id', 'package', 'installed_version', 'fixed_version', 'severity'], $parts);
        if (count($findings) >= 512) { $r['truncated'] = true; break; }
    }
    unset($r['output']);
    return $r + ['image' => $image, 'scanner' => 'trivy', 'status' => $r['exit_code'] !== 0 ? 'failed' : ($r['truncated'] ? 'partial' : 'succeeded'), 'findings' => $findings, 'complete' => $r['exit_code'] === 0 && !$r['truncated'], 'note' => 'The installed Trivy executable may download vulnerability databases and image metadata. A zero finding count is conclusive only when complete is true.'];
}
function ariaDiagnosticsRead(string $state, string $action, array $a): array {
    switch ($action) {
        case 'diagnostics_health': return ariaDiagHealth($state);
        case 'diagnostics_storage': return ariaDiagStorageSample($state);
        case 'diagnostics_shares': return ariaDiagShares();
        case 'diagnostics_permissions': return ariaDiagPermissions($a['path']);
        case 'diagnostics_container': return ariaDiagContainer($a['name']);
        case 'diagnostics_resources': return ariaDiagResources($state, false);
        case 'diagnostics_resource_history': return ariaDiagResources($state, true, $a['limit'] ?? 10);
        case 'diagnostics_history': return ariaDiagHistory($state, $a);
        case 'diagnostics_monitor': return ariaDiagnosticsMonitor($state);
        case 'diagnostics_network': return ariaDiagNetwork($a);
        case 'diagnostics_devices': return ariaDiagDevices();
        case 'diagnostics_device_assignments': return ariaDiagAssignments();
        case 'diagnostics_disks': return ariaDiagDisks();
        case 'diagnostics_vm_list': return ariaDiagVM([], 'list');
        case 'diagnostics_vm_inspect': return ariaDiagVM($a, 'inspect');
        case 'diagnostics_vm_config': return ariaDiagVMConfig(ariaDiagVMName($a['name']));
        case 'diagnostics_vm_snapshots': return ariaDiagCommand('virsh', ['--connect', 'qemu:///system', 'snapshot-list', '--domain', ariaDiagVMName($a['name'])], 3);
        case 'diagnostics_vm_snapshot_read': return ariaDiagSnapshotRead($a['name'], $a['snapshot']);
        case 'diagnostics_security_capabilities': return ['trivy_available' => is_executable(ariaDiagBin('trivy')), 'scanner' => is_executable(ariaDiagBin('trivy')) ? 'trivy' : null, 'note' => 'No scanner is installed automatically. Vulnerability scans are queued jobs.'];
        case 'diagnostics_cleanup_plan':
            $r = ariaDocker(['system', 'df', '--format', '{{json .}}'], 3, false);
            return ['note' => 'Docker-reported reclaimable estimates. No deletion was performed; active volume data is not inspected.'] + ariaDiagJsonLines($r, ['Type', 'TotalCount', 'Active', 'Size', 'Reclaimable']);
    }
    ariaFail('unsupported action');
}
function ariaDiagnosticsExecute(string $state, array $job): array {
    $a = $job['arguments'];
    switch ($job['action']) {
        case 'diagnostics_vm_action': return ariaDiagVM($a, $a['operation']);
        case 'diagnostics_vm_config_save': return ariaDiagVMWrite($state, $job);
        case 'diagnostics_vm_snapshot_create':
        case 'diagnostics_vm_snapshot_revert': return ariaDiagSnapshotExecute($state, $job);
        case 'diagnostics_smart': return ariaDiagSmartSample($state, $a['device']);
        case 'diagnostics_security_scan': return ariaDiagSecurity($a['image']);
    }
    ariaFail('unsupported action');
}
