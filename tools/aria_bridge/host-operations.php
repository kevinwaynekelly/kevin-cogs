<?php
/** Privileged, queued administration. Never pass command text to a shell. */
declare(strict_types=1);

const ARIA_OPS_FILE_MAX = 131072;
const ARIA_OPS_ARCHIVE_MAX = 16777216;
const ARIA_OPS_ARCHIVE_ENTRIES = 2048;

function ariaOpsDirectory(string $path): string {
    if (is_link($path)) ariaFail('invalid request');
    if (!is_dir($path) && !mkdir($path, 0700, true)) ariaFail('internal error');
    if (!chmod($path, 0700)) ariaFail('internal error');
    return $path;
}
function ariaOpsName(string $name): string {
    if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/D', $name)) ariaFail('invalid request');
    return $name;
}
function ariaOpsRoots(): array {
    $roots = ariaTest() ? getenv('ARIA_AGENT_FILE_ROOTS') : false;
    return $roots === false ? ['/boot/config', '/mnt/user', '/mnt/cache', '/etc', '/var/log', '/tmp'] : explode(':', $roots);
}
function ariaOpsPath(string $state, string $path, bool $exists = true): string {
    if ($path === '' || $path[0] !== '/' || strlen($path) > 4096 || preg_match('/[\x00-\x1f\x7f]/', $path)) ariaFail('invalid request');
    $parts = explode('/', substr($path, 1));
    if (array_intersect($parts, ['', '.', '..'])) ariaFail('invalid request');
    $allowed = false;
    foreach (ariaOpsRoots() as $root) if ($path === $root || strpos($path, $root.'/') === 0) $allowed = true;
    if (!$allowed || $path === $state || strpos($path, $state.'/') === 0) ariaFail('invalid request');
    $prefix = '';
    foreach ($parts as $part) {
        $prefix .= '/'.$part;
        if (is_link($prefix)) ariaFail('invalid request');
    }
    if ($exists && !file_exists($path)) ariaFail('not found');
    if (!$exists && !is_dir(dirname($path))) ariaFail('not found');
    if (file_exists($path) && !is_file($path) && !is_dir($path)) ariaFail('invalid request');
    return $path;
}
function ariaOpsStat(string $path): array {
    clearstatcache(true, $path);
    $s = lstat($path);
    if ($s === false) ariaFail('not found');
    $data = ['device' => $s['dev'], 'inode' => $s['ino'], 'size' => $s['size'], 'mtime' => $s['mtime'], 'ctime' => $s['ctime'], 'mode' => sprintf('%04o', $s['mode'] & 07777), 'uid' => $s['uid'], 'gid' => $s['gid'], 'kind' => is_link($path) ? 'symlink' : (is_dir($path) ? 'directory' : (is_file($path) ? 'file' : 'special'))];
    $data['signature'] = hash('sha256', ariaJson($data));
    return $data;
}
function ariaOpsCompare(string $path, string $expected): ?string {
    if (!file_exists($path)) {
        if ($expected !== '') ariaFail('hash mismatch');
        return null;
    }
    if (!is_file($path) || is_link($path)) ariaFail('invalid request');
    $content = ariaReadFile($path, ARIA_OPS_FILE_MAX);
    if ($expected === '' || !hash_equals(hash('sha256', $content), $expected)) ariaFail('hash mismatch');
    return $content;
}
function ariaOpsReplace(string $path, string $content, int $mode, ?array $owner = null): void {
    $owner = $owner ?? (is_file($path) ? ['uid' => fileowner($path), 'gid' => filegroup($path)] : null);
    $temp = dirname($path).'/.aria-ops-'.bin2hex(random_bytes(10));
    try {
        ariaAtomic($temp, $content, $mode);
        if ($owner !== null && ((!chown($temp, $owner['uid'])) || !chgrp($temp, $owner['gid']))) ariaFail('command failed');
        if (!rename($temp, $path)) ariaFail('internal error');
    } finally { if (file_exists($temp)) @unlink($temp); }
}
function ariaOpsBackup(string $state, string $id, string $path, string $content): string {
    ariaName($id, 'id');
    $dir = ariaOpsDirectory("$state/file-backups");
    if (file_exists("$dir/$id.json")) ariaFail('invalid request');
    ariaAtomic("$dir/$id.json", ariaJson(['backup_id' => $id, 'path' => $path, 'sha256' => hash('sha256', $content), 'mode' => fileperms($path) & 0777, 'uid' => fileowner($path), 'gid' => filegroup($path), 'created_at' => ariaNow(), 'content_base64' => base64_encode($content)]));
    return $id;
}
function ariaOperationsSecretValue(string $state, string $name): string {
    $path = ariaFile("$state/secrets", ariaOpsName($name).'.json');
    $data = json_decode(ariaReadFile($path, 131072), true);
    if (!is_array($data) || !is_string($data['value'] ?? null) || strlen($data['value']) > 16384) ariaFail('internal error');
    return $data['value'];
}
function ariaOperationsSecretValues(string $state): array {
    $values = [];
    $dashboardToken = "$state/dashboard-token";
    if (is_link($dashboardToken)) ariaFail('internal error');
    if (file_exists($dashboardToken)) {
        if (!is_file($dashboardToken) || (!ariaTest() && fileowner($dashboardToken) !== 0)) ariaFail('internal error');
        $token = trim(ariaReadFile($dashboardToken, 65));
        if (!preg_match('/^[a-f0-9]{64}$/D', $token)) ariaFail('internal error');
        $values[] = $token;
    }
    if (!is_dir("$state/secrets")) return $values;
    $paths = glob("$state/secrets/*.json") ?: [];
    if (count($paths) > 256) ariaFail('invalid request');
    foreach ($paths as $path) {
        $value = ariaOperationsSecretValue($state, basename($path, '.json')); $values[] = $value;
        $decoded = json_decode($value, true, 16);
        if (is_array($decoded)) array_walk_recursive($decoded, function ($v) use (&$values) { if (is_string($v) && $v !== '') $values[] = $v; });
    }
    return $values;
}
function ariaOpsScrub(string $state, string $text, int $limit = ARIA_MAX_OUTPUT): string {
    $values = ariaOperationsSecretValues($state);
    usort($values, function ($a, $b) { return strlen($b) <=> strlen($a); });
    foreach ($values as $value) if ($value !== '') $text = str_replace($value, '[REDACTED]', $text);
    return ariaScrub($text, [], $limit);
}
function ariaOpsEnvironment(string $state, array $references): array {
    if (count($references) > 32) ariaFail('invalid request');
    $environment = [];
    foreach ($references as $reference) {
        $name = $reference['environment'];
        if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]{0,63}$/D', $name) || isset($environment[$name])) ariaFail('invalid request');
        // Interpreter and loader settings are never accepted as credential names.
        if (preg_match('/^(?:LD_|BASH_ENV$|ENV$|SHELLOPTS$|BASHOPTS$|PATH$|PYTHONPATH$|PYTHONHOME$|PERL5OPT$|RUBYOPT$|NODE_OPTIONS$)/', $name)) ariaFail('invalid request');
        $value = ariaOperationsSecretValue($state, $reference['secret_ref']);
        if (strpos($value, "\0") !== false) ariaFail('invalid request');
        $environment[$name] = $value;
    }
    return $environment;
}
function ariaOpsRun(string $state, array $argv, int $timeout, ?string $cwd, array $environment = []): array {
    if (!$argv || count($argv) > 256 || !is_string($argv[0]) || $argv[0] === '' || $argv[0][0] !== '/') ariaFail('invalid request');
    foreach ($argv as $argument) if (!is_string($argument) || strlen($argument) > 4096 || strpos($argument, "\0") !== false) ariaFail('invalid request');
    if ($timeout < 1 || $timeout > 86400) ariaFail('invalid request');
    $original = [];
    try {
        foreach ($environment as $key => $value) { $original[$key] = getenv($key); putenv($key.'='.$value); }
        $result = ariaRun($argv, $timeout, $cwd);
    } finally {
        foreach ($original as $key => $value) putenv($value === false ? $key : $key.'='.$value);
    }
    // Scrub the values actually supplied even if the command rotated their files.
    $supplied = array_values($environment);
    usort($supplied, function ($a, $b) { return strlen($b) <=> strlen($a); });
    foreach ($supplied as $value) if ($value !== '') $result['output'] = str_replace($value, '[REDACTED]', $result['output']);
    $result['output'] = ariaOpsScrub($state, $result['output']);
    $result['redacted'] = true;
    return $result;
}
function ariaOpsSyntax(string $state, string $id, string $script): void {
    if (strlen($script) > ARIA_OPS_FILE_MAX || strpos($script, "\0") !== false) ariaFail('invalid request');
    $path = "$state/jobs/$id.syntax";
    ariaAtomic($path, $script);
    try {
        $result = ariaRun(['/bin/bash', '-n', $path], 5);
        if ($result['exit_code'] !== 0) ariaFail('invalid request');
    } finally { @unlink($path); }
}
function ariaOpsScriptDestination(string $name): string {
    ariaName($name, 'script');
    $base = ariaScripts(); $dir = "$base/$name";
    if (!is_dir($base) || is_link($base) || is_link($dir) || (file_exists($dir) && !is_dir($dir))) ariaFail('invalid request');
    if (file_exists("$dir/script") && (!is_file("$dir/script") || is_link("$dir/script"))) ariaFail('invalid request');
    return "$dir/script";
}
function ariaOpsConfigEntries(bool $withData = false): array {
    $entries = []; $total = 0;
    foreach (['template' => glob(ariaTemplates().'/*.xml') ?: [], 'script' => glob(ariaScripts().'/*/script') ?: []] as $kind => $paths) {
        foreach ($paths as $path) {
            if (count($entries) >= ARIA_OPS_ARCHIVE_ENTRIES) ariaFail('invalid request');
            $name = $kind === 'template' ? basename($path) : basename(dirname($path));
            $path = $kind === 'template' ? ariaTemplatePath($name) : ariaScriptPath($name);
            $content = ariaReadFile($path, ARIA_OPS_FILE_MAX); $total += strlen($content);
            if ($total > ARIA_OPS_ARCHIVE_MAX / 2) ariaFail('invalid request');
            $entry = ['kind' => $kind, 'name' => $name, 'sha256' => hash('sha256', $content), 'bytes' => strlen($content), 'mode' => fileperms($path) & 0777];
            if ($withData) $entry['content_base64'] = base64_encode($content);
            $entries[] = $entry;
        }
    }
    usort($entries, function ($a, $b) { return strcmp($a['kind'].'/'.$a['name'], $b['kind'].'/'.$b['name']); });
    return $entries;
}
function ariaOpsInventory(): array {
    $entries = ariaOpsConfigEntries();
    return ['inventory_sha256' => hash('sha256', ariaJson($entries)), 'entries' => $entries];
}
function ariaOpsArchiveCreate(string $state, string $id): array {
    ariaName($id, 'id');
    $dir = ariaOpsDirectory("$state/config-archives");
    if (file_exists("$dir/$id.json.gz")) ariaFail('invalid request');
    $entries = ariaOpsConfigEntries(true);
    $data = ariaJson(['version' => 1, 'created_at' => ariaNow(), 'entries' => $entries]);
    if (strlen($data) > ARIA_OPS_ARCHIVE_MAX) ariaFail('invalid request');
    $compressed = gzencode($data, 6);
    if ($compressed === false) ariaFail('internal error');
    $metadata = ['archive_id' => $id, 'sha256' => hash('sha256', $compressed), 'created_at' => ariaNow(), 'entry_count' => count($entries), 'bytes' => strlen($compressed), 'uncompressed_bytes' => strlen($data), 'scope' => 'Docker templates and User Scripts script files; excludes application data and vault'];
    ariaAtomic("$dir/$id.json.gz", $compressed);
    ariaAtomic("$dir/$id.json", ariaJson($metadata));
    return $metadata;
}
function ariaOpsArchiveRead(string $state, string $id, string $expected): array {
    $path = ariaFile("$state/config-archives", ariaName($id, 'id').'.json.gz');
    $bytes = ariaReadFile($path, ARIA_OPS_ARCHIVE_MAX);
    if (!hash_equals($expected, hash('sha256', $bytes))) ariaFail('hash mismatch');
    $json = @gzdecode($bytes, ARIA_OPS_ARCHIVE_MAX);
    $archive = $json === false ? null : json_decode($json, true);
    if (!is_array($archive) || ($archive['version'] ?? null) !== 1 || !is_array($archive['entries'] ?? null) || count($archive['entries']) > ARIA_OPS_ARCHIVE_ENTRIES) ariaFail('invalid request');
    $seen = []; $total = 0;
    foreach ($archive['entries'] as &$entry) {
        if (!is_array($entry) || !in_array($entry['kind'] ?? null, ['template', 'script'], true)) ariaFail('invalid request');
        $entry['name'] = ariaName($entry['name'] ?? null, $entry['kind']);
        $key = $entry['kind'].'/'.$entry['name'];
        if (isset($seen[$key]) || !is_string($entry['content_base64'] ?? null) || !is_string($entry['sha256'] ?? null)) ariaFail('invalid request');
        $seen[$key] = true;
        $content = base64_decode($entry['content_base64'], true);
        if ($content === false || strlen($content) > ARIA_OPS_FILE_MAX || !hash_equals($entry['sha256'], hash('sha256', $content))) ariaFail('invalid request');
        $total += strlen($content);
        if ($total > ARIA_OPS_ARCHIVE_MAX / 2) ariaFail('invalid request');
        if ($entry['kind'] === 'template') ariaXML($content);
        $entry['content'] = $content;
    }
    unset($entry);
    return $archive;
}
function ariaOperationsRead(string $state, string $action, array $a): array {
    switch ($action) {
        case 'operations_file_roots': return ['roots' => ariaOpsRoots(), 'symlinks_followed' => false, 'max_file_bytes' => ARIA_OPS_FILE_MAX];
        case 'operations_file_stat':
            $path = ariaOpsPath($state, $a['path']);
            return ['path' => $path, 'stat' => ariaOpsStat($path)];
        case 'operations_file_list':
            $path = ariaOpsPath($state, $a['path']);
            if (!is_dir($path)) ariaFail('invalid request');
            $rows = []; $skip = $a['offset'] ?? 0; $limit = $a['limit'] ?? 100; $more = false;
            $iterator = new DirectoryIterator($path);
            foreach ($iterator as $item) {
                if ($item->isDot()) continue;
                if ($skip > 0) { $skip--; continue; }
                if (count($rows) >= $limit) { $more = true; break; }
                $rows[] = ['name' => $item->getFilename(), 'stat' => ariaOpsStat($item->getPathname())];
            }
            return ['path' => $path, 'entries' => $rows, 'next_offset' => $more ? ($a['offset'] ?? 0) + count($rows) : null, 'ordering' => 'filesystem order; repeat a page if directory changes'];
        case 'operations_file_read':
            $path = ariaOpsPath($state, $a['path']);
            if (!is_file($path) || preg_match('~(?:^|/)(?:shadow|gshadow|id_(?:rsa|ed25519|ecdsa)|[^/]*\.(?:key|pem))$~i', $path)) ariaFail('invalid request');
            $bytes = ariaReadFile($path, ARIA_OPS_FILE_MAX);
            if (strpos($bytes, "\0") !== false || !preg_match('//u', $bytes)) ariaFail('invalid request');
            return ['path' => $path, 'sha256' => hash('sha256', $bytes), 'bytes' => strlen($bytes), 'content' => ariaOpsScrub($state, $bytes), 'redacted' => true, 'truncated' => strlen($bytes) > ARIA_MAX_OUTPUT];
        case 'operations_secret_list':
            $rows = [];
            foreach (array_slice(glob("$state/secrets/*.json") ?: [], 0, 256) as $path) {
                $path = ariaFile("$state/secrets", basename($path));
                $secret = json_decode(ariaReadFile($path, 131072), true);
                $rows[] = ['name' => $secret['name'], 'revision' => $secret['revision'], 'updated_at' => $secret['updated_at']];
            }
            return ['secrets' => $rows, 'values_returned' => false];
        case 'operations_archive_list':
            $rows = [];
            foreach (array_slice(glob("$state/config-archives/*.json") ?: [], -100) as $path) {
                $rows[] = ariaReadJson(ariaFile("$state/config-archives", basename($path)));
            }
            return ['archives' => $rows, 'limit' => 100];
    }
    ariaFail('unsupported action');
}
function ariaOperationsExecute(string $state, array $job): array {
    $a = $job['arguments']; $id = ariaName($job['job_id'], 'id');
    switch ($job['action']) {
        case 'operations_host_exec':
            $cwd = isset($a['working_directory']) ? ariaOpsPath($state, $a['working_directory']) : null;
            if ($cwd !== null && !is_dir($cwd)) ariaFail('invalid request');
            return ariaOpsRun($state, $a['argv'], $a['timeout_seconds'] ?? 300, $cwd, ariaOpsEnvironment($state, $a['secret_environment'] ?? []));
        case 'operations_container_exec':
            $name = ariaName($a['name'], 'container');
            if (ariaProtected($name)) ariaFail('protected container');
            $info = ariaInspect($name);
            $environment = ariaOpsEnvironment($state, $a['secret_environment'] ?? []);
            $argv = [ariaPath('ARIA_AGENT_DOCKER', '/usr/bin/docker'), 'exec'];
            if (isset($a['user'])) { $argv[] = '--user'; $argv[] = $a['user']; }
            if (isset($a['working_directory'])) { $argv[] = '--workdir'; $argv[] = $a['working_directory']; }
            foreach (array_keys($environment) as $key) { $argv[] = '--env'; $argv[] = $key; }
            $argv[] = $info['Id'];
            if (!$a['argv'] || $a['argv'][0] === '' || $a['argv'][0][0] !== '/') ariaFail('invalid request');
            $result = ariaOpsRun($state, array_merge($argv, $a['argv']), $a['timeout_seconds'] ?? 300, null, $environment);
            $values = [];
            foreach ($info['Config']['Env'] ?? [] as $entry) $values[] = explode('=', $entry, 2)[1] ?? '';
            $result['output'] = ariaScrub($result['output'], $values);
            $result['name'] = $name;
            $result['container_process_may_continue'] = $result['timed_out'];
            return $result;
        case 'operations_script_save':
            $path = ariaOpsScriptDestination($a['name']);
            $old = ariaOpsCompare($path, $a['expected_sha256']);
            if (strpos($a['script'], '[REDACTED]') !== false || strpos($a['script'], '__ARIA_REDACTED_') !== false) ariaFail('invalid request');
            ariaOpsSyntax($state, $id, $a['script']);
            $backup = $old === null ? null : ariaOpsBackup($state, $id, $path, $old);
            if (!is_dir(dirname($path)) && !mkdir(dirname($path), 0700)) ariaFail('internal error');
            ariaOpsCompare($path, $a['expected_sha256']);
            ariaOpsReplace($path, $a['script'], 0700);
            return ['name' => $a['name'], 'sha256' => hash('sha256', $a['script']), 'backup_id' => $backup, 'syntax_valid' => true];
        case 'operations_script_run_arguments':
            $path = ariaScriptPath($a['name']);
            $script = ariaOpsCompare($path, $a['expected_sha256']);
            $copy = "$state/jobs/$id.script";
            ariaAtomic($copy, $script, 0700);
            try { $result = ariaOpsRun($state, array_merge(['/bin/bash', $copy], $a['arguments'] ?? []), $a['timeout_seconds'] ?? 3600, dirname($path), ariaOpsEnvironment($state, $a['secret_environment'] ?? [])); }
            finally { @unlink($copy); }
            $result['name'] = $a['name'];
            return $result;
        case 'operations_secret_import':
            $name = ariaOpsName($a['name']);
            $source = ariaOpsPath($state, $a['source_path']);
            if (!is_file($source)) ariaFail('invalid request');
            $value = ariaReadFile($source, 16384);
            if (!hash_equals($a['expected_source_sha256'], hash('sha256', $value))) ariaFail('hash mismatch');
            if ($value === '' || strpos($value, "\0") !== false || !preg_match('//u', $value)) ariaFail('invalid request');
            $dir = ariaOpsDirectory("$state/secrets"); $path = ariaFile($dir, $name.'.json', false);
            if (is_file($path)) {
                $old = ariaReadJson($path);
                if (!hash_equals($old['revision'], $a['expected_revision'])) ariaFail('hash mismatch');
            } elseif ($a['expected_revision'] !== '') ariaFail('hash mismatch');
            if (!is_file($path) && count(glob("$dir/*.json") ?: []) >= 256) ariaFail('invalid request');
            $data = ['name' => $name, 'revision' => bin2hex(random_bytes(16)), 'updated_at' => ariaNow(), 'value' => $value];
            ariaAtomic($path, ariaJson($data)); unset($data['value']);
            return $data;
        case 'operations_secret_delete':
            $path = ariaFile("$state/secrets", ariaOpsName($a['name']).'.json');
            $old = ariaReadJson($path);
            if (!hash_equals($old['revision'], $a['expected_revision'])) ariaFail('hash mismatch');
            if (!unlink($path)) ariaFail('internal error');
            return ['name' => $a['name'], 'deleted' => true];
        case 'operations_file_write':
            $path = ariaOpsPath($state, $a['path'], false);
            $old = ariaOpsCompare($path, $a['expected_sha256']);
            if (strlen($a['content']) > ARIA_OPS_FILE_MAX || strpos($a['content'], "\0") !== false || strpos($a['content'], '[REDACTED]') !== false || strpos($a['content'], '__ARIA_REDACTED_') !== false) ariaFail('invalid request');
            $backup = $old === null ? null : ariaOpsBackup($state, $id, $path, $old);
            $mode = $old === null ? 0600 : fileperms($path) & 0777;
            ariaOpsCompare($path, $a['expected_sha256']);
            ariaOpsReplace($path, $a['content'], $mode);
            return ['path' => $path, 'sha256' => hash('sha256', $a['content']), 'backup_id' => $backup];
        case 'operations_file_mkdir':
            $path = ariaOpsPath($state, $a['path'], false);
            if (file_exists($path)) ariaFail('invalid request');
            $mode = octdec($a['mode'] ?? '0755');
            if (!mkdir($path, $mode) || !chmod($path, $mode)) ariaFail('internal error');
            return ['path' => $path, 'stat' => ariaOpsStat($path)];
        case 'operations_file_permissions':
            $path = ariaOpsPath($state, $a['path']);
            $old = ariaOpsStat($path);
            if (!hash_equals($old['signature'], $a['expected_signature'])) ariaFail('hash mismatch');
            if (isset($a['uid']) && !chown($path, $a['uid'])) ariaFail('command failed');
            if (isset($a['gid']) && !chgrp($path, $a['gid'])) ariaFail('command failed');
            if (!chmod($path, octdec($a['mode']))) ariaFail('command failed');
            return ['path' => $path, 'before' => $old, 'after' => ariaOpsStat($path), 'recursive' => false];
        case 'operations_file_backup':
            $path = ariaOpsPath($state, $a['path']);
            $content = ariaOpsCompare($path, $a['expected_sha256']);
            return ['backup_id' => ariaOpsBackup($state, $id, $path, $content), 'path' => $path, 'sha256' => $a['expected_sha256']];
        case 'operations_file_restore':
            $backup = ariaReadJson(ariaFile("$state/file-backups", ariaName($a['backup_id'], 'id').'.json'));
            $path = ariaOpsPath($state, $backup['path'], false);
            $old = ariaOpsCompare($path, $a['expected_sha256']);
            $content = base64_decode($backup['content_base64'], true);
            if ($content === false || strlen($content) > ARIA_OPS_FILE_MAX || !hash_equals($backup['sha256'], hash('sha256', $content))) ariaFail('invalid request');
            $previous = $old === null ? null : ariaOpsBackup($state, $id, $path, $old);
            ariaOpsCompare($path, $a['expected_sha256']);
            ariaOpsReplace($path, $content, $backup['mode'] & 0777, ['uid' => $backup['uid'], 'gid' => $backup['gid']]);
            return ['path' => $path, 'sha256' => hash('sha256', $content), 'backup_id' => $previous];
        case 'operations_config_inventory': return ariaOpsInventory();
        case 'operations_archive_create': return ariaOpsArchiveCreate($state, $id);
        case 'operations_archive_verify':
            $archive = ariaOpsArchiveRead($state, $a['archive_id'], $a['expected_sha256']);
            foreach ($archive['entries'] as $entry) if ($entry['kind'] === 'script') ariaOpsSyntax($state, $id, $entry['content']);
            return ['archive_id' => $a['archive_id'], 'verified' => true, 'entry_count' => count($archive['entries']), 'scope' => 'archive integrity and config syntax; applications were not started'];
        case 'operations_archive_restore':
            $archive = ariaOpsArchiveRead($state, $a['archive_id'], $a['expected_sha256']);
            if (!hash_equals(ariaOpsInventory()['inventory_sha256'], $a['expected_inventory_sha256'])) ariaFail('hash mismatch');
            $plan = [];
            foreach ($archive['entries'] as $entry) {
                $path = $entry['kind'] === 'template' ? ariaTemplatePath($entry['name'], false) : ariaOpsScriptDestination($entry['name']);
                if ($entry['kind'] === 'script') ariaOpsSyntax($state, $id, $entry['content']);
                $old = is_file($path) ? ariaReadFile($path, ARIA_OPS_FILE_MAX) : null;
                if ($old !== null && hash_equals($old, $entry['content'])) continue;
                if ($entry['kind'] === 'template' && (ariaProtected((string)ariaXML($entry['content'])->Name) || ($old !== null && ariaProtected((string)ariaXML($old)->Name)))) ariaFail('protected container');
                if ($old !== null && ($a['overwrite'] ?? false) !== true) ariaFail('invalid request');
                $plan[] = ['path' => $path, 'old' => $old, 'content' => $entry['content'], 'mode' => $old === null ? ($entry['kind'] === 'script' ? 0700 : 0600) : fileperms($path) & 0777];
            }
            $backup = ariaOpsArchiveCreate($state, $id);
            if (!hash_equals(ariaOpsInventory()['inventory_sha256'], $a['expected_inventory_sha256'])) ariaFail('hash mismatch');
            $written = [];
            try {
                foreach ($plan as $item) {
                    if (!is_dir(dirname($item['path'])) && !mkdir(dirname($item['path']), 0700)) ariaFail('internal error');
                    ariaOpsCompare($item['path'], $item['old'] === null ? '' : hash('sha256', $item['old']));
                    ariaOpsReplace($item['path'], $item['content'], $item['mode']);
                    $written[] = $item;
                }
            } catch (Throwable $e) {
                $rollback = true;
                foreach (array_reverse($written) as $item) {
                    try {
                        ariaOpsCompare($item['path'], hash('sha256', $item['content']));
                        if ($item['old'] === null) { if (!unlink($item['path'])) ariaFail('internal error'); }
                        else ariaOpsReplace($item['path'], $item['old'], $item['mode']);
                    } catch (Throwable $ignored) { $rollback = false; }
                }
                if (!$rollback) ariaFail('rollback failed');
                throw $e;
            }
            return ['archive_id' => $a['archive_id'], 'restored_entries' => count($written), 'previous_archive' => $backup, 'deployed_containers' => false, 'inventory_sha256' => ariaOpsInventory()['inventory_sha256']];
    }
    ariaFail('unsupported action');
}
