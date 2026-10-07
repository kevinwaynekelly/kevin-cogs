<?php
/**
 * Aria's root-only Unraid worker. The public bridge receives only a Unix socket.
 * No Docker socket, host filesystem, shell text API, or state directory is mounted
 * in that bridge. Installed templates and installed User Scripts are privileged.
 *
 * php -d short_open_tag=On host-agent.php serve STATE_DIR SOCKET_PATH
 * php -d short_open_tag=On host-agent.php work STATE_DIR
 *
 * Native conversion follows unraid/webgui's Helpers.php::xmlToCommand and
 * CreateDocker.php. We deliberately reject native Tailscale templates because
 * their entrypoint injection needs the Web UI's separate provisioning sequence.
 */
declare(strict_types=1);
require_once __DIR__.'/host-extensions.php';

const ARIA_MAX_INPUT = 1048576;
const ARIA_MAX_XML = 131072;
const ARIA_MAX_OUTPUT = 65536;
const ARIA_MAX_JOBS = 512;
const ARIA_MAX_PENDING = 128;
const ARIA_MAX_RECEIPTS = 100000;
const ARIA_PROTECTED = ['aria-gpt-bridge', 'cloudflared', 'haproxy'];
const ARIA_ERRORS = [
    'invalid request', 'unsupported action', 'not found', 'invalid template',
    'hash mismatch', 'template exists', 'request_id conflict', 'job unknown',
    'queue full', 'deduplication capacity reached', 'command failed',
    'operation timed out', 'protected container', 'native template converter unavailable',
    'unsupported template feature', 'rollback failed', 'internal error',
    'management busy', 'bridge update unavailable',
    'policy denied', 'healthcheck required', 'appdata in use', 'unsupported appdata entry',
    'copy budget exceeded', 'copy verification failed',
    'application credential unavailable',
    'application transport unavailable',
    'application connection failed',
    'application redirect refused',
    'application authentication failed',
    'application operation unavailable',
    'application request failed',
    'application response too large',
    'application invalid response',
    'application outcome unknown',
    'operation outcome unknown',
];

function ariaFail(string $message): void { throw new RuntimeException($message); }
function ariaTest(): bool { return getenv('ARIA_AGENT_TEST_MODE') === '1'; }
function ariaPath(string $key, string $normal): string {
    return ariaTest() && getenv($key) !== false ? (string)getenv($key) : $normal;
}
function ariaTemplates(): string {
    return ariaPath('ARIA_AGENT_TEMPLATES', '/boot/config/plugins/dockerMan/templates-user');
}
function ariaScripts(): string {
    return ariaPath('ARIA_AGENT_SCRIPTS', '/boot/config/plugins/user.scripts/scripts');
}
function ariaNow(): string { return gmdate('Y-m-d\TH:i:s\Z'); }
function ariaJson($data): string {
    $result = json_encode($data, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
    if ($result === false) ariaFail('internal error');
    return $result;
}
function ariaAtomic(string $path, string $content, int $mode = 0600): void {
    $temp = dirname($path).'/.aria-'.bin2hex(random_bytes(10));
    $handle = @fopen($temp, 'x');
    if ($handle === false) ariaFail('internal error');
    try {
        if (!chmod($temp, $mode) || fwrite($handle, $content) !== strlen($content)) ariaFail('internal error');
        fflush($handle);
        if (function_exists('fsync')) fsync($handle);
        fclose($handle);
        $handle = null;
        if (!rename($temp, $path)) ariaFail('internal error');
    } finally {
        if (is_resource($handle)) fclose($handle);
        if (file_exists($temp)) unlink($temp);
    }
}
function ariaInit(string $state): void {
    umask(0077);
    if (!ariaTest() && function_exists('posix_geteuid') && posix_geteuid() !== 0) ariaFail('internal error');
    foreach ([$state, "$state/jobs", "$state/requests", "$state/backups", "$state/bridge-updates"] as $dir) {
        if (is_link($dir)) ariaFail('internal error');
        if (!is_dir($dir) && !mkdir($dir, 0700, true)) ariaFail('internal error');
        if (!chmod($dir, 0700)) ariaFail('internal error');
    }
}
function ariaLock(string $state, string $name, bool $nonblock = false) {
    // Detached bridge updaters must not inherit service or queue locks across
    // exec, or replacing the host service would deadlock on its own old locks.
    $handle = fopen("$state/$name.lock", 'ce');
    if ($handle === false || !flock($handle, LOCK_EX | ($nonblock ? LOCK_NB : 0))) ariaFail('internal error');
    return $handle;
}
function ariaUnlock($handle): void { flock($handle, LOCK_UN); fclose($handle); }
function ariaReadJson(string $path): array {
    $value = json_decode((string)@file_get_contents($path), true);
    if (!is_array($value)) ariaFail('internal error');
    return $value;
}
function ariaName($name, string $kind): string {
    $patterns = [
        'id' => '/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/D',
        'container' => '/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/D',
        'template' => '/^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}\.xml$/D',
        'script' => '/^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$/D',
    ];
    if (!is_string($name) || !preg_match($patterns[$kind], $name) || ($kind === 'template' && strlen($name) > 128)) ariaFail('invalid request');
    return $name;
}
function ariaFile(string $base, string $name, bool $required = true): string {
    $root = realpath($base);
    $path = $base.'/'.$name;
    if ($root === false || is_link($base) || is_link($path)) ariaFail('not found');
    if (file_exists($path) && (!is_file($path) || realpath(dirname($path)) !== $root)) ariaFail('not found');
    if ($required && !is_file($path)) ariaFail('not found');
    return $path;
}
function ariaTemplatePath(string $name, bool $required = true): string {
    return ariaFile(ariaTemplates(), ariaName($name, 'template'), $required);
}
function ariaScriptPath(string $name): string {
    $name = ariaName($name, 'script');
    $base = ariaScripts();
    $dir = "$base/$name";
    if (is_link($base) || is_link($dir) || realpath(dirname($dir)) !== realpath($base)) ariaFail('not found');
    return ariaFile($dir, 'script');
}
function ariaReadFile(string $path, int $max): string {
    if (filesize($path) > $max) ariaFail('invalid request');
    $data = file_get_contents($path);
    if ($data === false || strlen($data) > $max) ariaFail('invalid request');
    return $data;
}
function ariaHash(string $path, string $expected): void {
    if (!preg_match('/^[a-f0-9]{64}$/D', $expected) || !hash_equals($expected, hash_file('sha256', $path))) ariaFail('hash mismatch');
}
function ariaXML(string $xml): SimpleXMLElement {
    if (strlen($xml) > ARIA_MAX_XML || preg_match('/<!DOCTYPE|<!ENTITY/i', $xml)) ariaFail('invalid template');
    libxml_use_internal_errors(true);
    $tree = simplexml_load_string($xml, 'SimpleXMLElement', LIBXML_NONET);
    libxml_clear_errors();
    if ($tree === false || $tree->getName() !== 'Container') ariaFail('invalid template');
    try { ariaName((string)$tree->Name, 'container'); } catch (Throwable $e) { ariaFail('invalid template'); }
    if (!preg_match('/^[a-zA-Z0-9][a-zA-Z0-9._\/:@-]{0,511}$/D', (string)$tree->Repository)) ariaFail('invalid template');
    return $tree;
}
function ariaRequestedNetwork(SimpleXMLElement $tree): string {
    return (string)$tree->Network !== '' ? (string)$tree->Network : (string)$tree->Networking->Mode;
}

/** All environment values are opaque, including innocuously named API tokens. */
function ariaRedactedXML(string $xml): array {
    $tree = ariaXML($xml);
    $secrets = [];
    foreach ($tree->xpath('//*') as $index => $node) {
        $name = $node->getName();
        $sensitive = in_array($name, ['ExtraParams', 'PostArgs', 'WebUI', 'TailscaleParams', 'TailscaleDParams'], true)
            || preg_match('/pass(word|wd)?|token|secret|credential|api.?key|auth/i', $name)
            || ($name === 'Config' && (in_array(strtolower((string)$node['Type']), ['variable', 'label'], true) || strtolower((string)$node['Mask']) === 'true'))
            || ($name === 'Value' && count($node->xpath('parent::Variable')) > 0);
        if ($sensitive && count($node->children()) === 0 && (string)$node !== '') {
            $token = '__ARIA_REDACTED_'.hash('sha256', 'node:'.$index).'__';
            $secrets[$token] = (string)$node;
            $node[0] = $token;
        }
        foreach ($node->attributes() as $key => $value) {
            if (($sensitive && in_array($key, ['Default', 'Value'], true)) || preg_match('/pass(word|wd)?|token|secret|credential|api.?key/i', $key)) {
                if ((string)$value === '') continue;
                $token = '__ARIA_REDACTED_'.hash('sha256', 'attr:'.$index.':'.$key).'__';
                $secrets[$token] = (string)$value;
                $node[$key] = $token;
            }
        }
    }
    return [(string)$tree->asXML(), $secrets];
}
function ariaRestoreXML(string $xml, ?string $old): string {
    $tree = ariaXML($xml);
    $secrets = $old === null ? [] : ariaRedactedXML($old)[1];
    foreach ($tree->xpath('//*') as $node) {
        if (count($node->children()) === 0 && isset($secrets[(string)$node])) $node[0] = $secrets[(string)$node];
        foreach ($node->attributes() as $key => $value) {
            if (isset($secrets[(string)$value])) $node[$key] = $secrets[(string)$value];
        }
    }
    $restored = (string)$tree->asXML();
    if (strpos($restored, '__ARIA_REDACTED_') !== false) ariaFail('invalid template');
    return $restored;
}
function ariaScrub(string $text, array $secrets = [], int $limit = ARIA_MAX_OUTPUT): string {
    usort($secrets, function ($a, $b) { return strlen((string)$b) <=> strlen((string)$a); });
    foreach ($secrets as $value) if (strlen((string)$value) >= 4) $text = str_replace((string)$value, '[REDACTED]', $text);
    $text = preg_replace('~(https?://)[^\s/@:]+:[^\s/@]+@~i', '$1[REDACTED]@', $text);
    $text = preg_replace('/\b(Bearer\s+)[A-Za-z0-9._~+\/-]+=*/i', '$1[REDACTED]', $text);
    $text = preg_replace('/((?:password|passwd|token|secret|api[_-]?key|authorization)\s*[=:]\s*)[^\s,;]+/i', '$1[REDACTED]', $text);
    return substr((string)$text, -$limit);
}
function ariaRun(array $argv, int $seconds = 30, ?string $cwd = null): array {
    $timeout = ariaPath('ARIA_AGENT_TIMEOUT', '/usr/bin/timeout');
    $command = array_merge([$timeout, '--signal=TERM', '--kill-after=5', (string)$seconds], $argv);
    $pipes = [];
    $process = proc_open($command, [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, $cwd, null, ['bypass_shell' => true]);
    if (!is_resource($process)) ariaFail('command failed');
    $initial = proc_get_status($process);
    $processId = $initial['pid'];
    $workerState = $GLOBALS['ARIA_WORKER_STATE'] ?? null;
    $processRecord = $GLOBALS['ARIA_PROCESS_RECORD'] ?? 'active-process.json';
    if (!in_array($processRecord, ['active-process.json', 'active-automation-process.json', 'active-serve-process.json'], true)) ariaFail('internal error');
    if ($workerState !== null) {
        $stat = (string)@file_get_contents('/proc/'.$processId.'/stat');
        $fields = preg_split('/\s+/', trim(substr($stat, (int)strrpos($stat, ')') + 1)));
        ariaAtomic($workerState.'/'.$processRecord, ariaJson(['pid' => $processId, 'start_time' => $fields[19] ?? '', 'started_at' => ariaNow()]));
    }
    foreach ($pipes as $pipe) stream_set_blocking($pipe, false);
    $output = ''; $truncated = false; $exit = -1; $status = $initial;
    $commandStarted = microtime(true); $lastProgress = 0; $outputBytes = 0;
    do {
        foreach ($pipes as $pipe) {
            $chunk = stream_get_contents($pipe, 8192);
            if ($chunk !== false) { $output .= $chunk; $outputBytes += strlen($chunk); }
        }
        if (strlen($output) > ARIA_MAX_OUTPUT) { $output = substr($output, -ARIA_MAX_OUTPUT); $truncated = true; }
        if ($workerState !== null && isset($GLOBALS['ARIA_CURRENT_JOB_ID']) && microtime(true) - $lastProgress >= 1) {
            $progressPath = $workerState.'/jobs/'.$GLOBALS['ARIA_CURRENT_JOB_ID'].'.json';
            if (is_file($progressPath)) {
                $progressJob = ariaReadJson($progressPath);
                if (($progressJob['status'] ?? '') === 'running') {
                    // Never publish command arguments or raw, not-yet-redacted output.
                    $progressJob['progress'] = ['updated_at' => ariaNow(), 'command_elapsed_seconds' => (int)(microtime(true)-$commandStarted), 'output_bytes_observed' => $outputBytes, 'output_truncated' => $truncated];
                    ariaAtomic($progressPath, ariaJson($progressJob));
                }
            }
            $lastProgress = microtime(true);
        }
        if (!$status['running']) { $exit = $status['exitcode']; break; }
        usleep(20000);
        $status = proc_get_status($process);
    } while (true);
    // A background child may still hold stdout open after bash exits. Reap its
    // process group before draining, so a writer cannot keep this loop alive.
    if (function_exists('posix_kill')) {
        @posix_kill(-$processId, 15);
        usleep(100000);
        @posix_kill(-$processId, 9);
    }
    foreach ($pipes as $pipe) {
        for ($drain = 0; $drain < 32 && ($chunk = stream_get_contents($pipe, 8192)) !== false && $chunk !== ''; $drain++) {
            $output .= $chunk;
            if (strlen($output) > ARIA_MAX_OUTPUT) { $output = substr($output, -ARIA_MAX_OUTPUT); $truncated = true; }
        }
        fclose($pipe);
    }
    $closed = proc_close($process);
    if ($workerState !== null) @unlink($workerState.'/'.$processRecord);
    if ($exit < 0) $exit = $closed;
    if (strlen($output) > ARIA_MAX_OUTPUT) $truncated = true;
    return ['exit_code' => $exit, 'output' => substr($output, -ARIA_MAX_OUTPUT), 'truncated' => $truncated, 'timed_out' => in_array($exit, [124, 137], true)];
}
function ariaDocker(array $args, int $seconds = 30, bool $mustSucceed = true): array {
    $r = ariaRun(array_merge([ariaPath('ARIA_AGENT_DOCKER', '/usr/bin/docker')], $args), $seconds);
    if ($mustSucceed && $r['exit_code'] !== 0) ariaFail($r['timed_out'] ? 'operation timed out' : 'command failed');
    return $r;
}
function ariaInspect(string $name, bool $required = true): ?array {
    $r = ariaDocker(['inspect', '--type', 'container', $name], 30, false);
    if ($r['exit_code'] !== 0) {
        if (preg_match('/No such (?:object|container)/i', $r['output'])) {
            if ($required) ariaFail('not found');
            return null;
        }
        ariaFail($r['timed_out'] ? 'operation timed out' : 'command failed');
    }
    $value = json_decode($r['output'], true);
    if (!is_array($value) || !isset($value[0]['Id'])) ariaFail('command failed');
    return $value[0];
}
function ariaContainers(): array {
    // Project before capture so unused labels and metadata do not exhaust the output limit.
    $format = '{"ID":{{json .ID}},"Names":{{json .Names}},"Image":{{json .Image}},"State":{{json .State}},"Status":{{json .Status}},"Ports":{{json .Ports}}}';
    $r = ariaDocker(['ps', '-a', '--format', $format]);
    if ($r['truncated']) ariaFail('command failed');
    $rows = [];
    foreach (explode("\n", trim($r['output'])) as $line) {
        if ($line === '') continue;
        $row = json_decode($line, true);
        if (!is_array($row)) ariaFail('command failed');
        $rows[] = array_intersect_key($row, array_flip(['ID', 'Names', 'Image', 'State', 'Status', 'Ports']));
    }
    return $rows;
}
function ariaTemplateList(): array {
    $rows = [];
    foreach (glob(ariaTemplates().'/*.xml') ?: [] as $path) {
        try {
            $file = ariaTemplatePath(basename($path)); $xml = ariaReadFile($file, ARIA_MAX_XML); $tree = ariaXML($xml);
            $rows[] = ['template' => basename($file), 'name' => (string)$tree->Name, 'image' => (string)$tree->Repository, 'sha256' => hash('sha256', $xml)];
        } catch (Throwable $e) { /* Invalid/unreadable templates cannot be deployed. */ }
    }
    return $rows;
}
function ariaProtected(string $name, bool $bulk = false): bool {
    return in_array(strtolower($name), $bulk ? ARIA_PROTECTED : ['aria-gpt-bridge'], true) || strpos(strtolower($name), 'aria-backup-') === 0;
}
function ariaFindTemplate(string $name): array {
    $matches = array_values(array_filter(ariaTemplateList(), function ($row) use ($name) { return $row['name'] === $name; }));
    if (count($matches) !== 1) ariaFail('not found');
    return $matches[0];
}
function ariaValidate(string $action, array $a): array {
    $extension = ariaExtensionValidate($action, $a);
    if ($extension !== null) return strpos($action, 'automation_') === 0 ? ariaAutomationValidate($action, $extension) : $extension;
    $fields = [
        'capabilities' => [], 'containers' => [], 'container_inspect' => ['name'], 'container_logs' => ['name', 'tail'],
        'templates_list' => [], 'template_get' => ['template'], 'scripts_list' => [], 'script_get' => ['name'], 'job_status' => ['job_id'],
        'container_action' => ['name', 'action', 'request_id'], 'template_save' => ['template', 'xml', 'expected_sha256', 'request_id'],
        'template_deploy' => ['template', 'expected_sha256', 'pull', 'start', 'request_id'],
        'script_run' => ['name', 'expected_sha256', 'timeout_seconds', 'request_id'],
        'container_update' => ['name', 'request_id'], 'containers_update_all' => ['request_id'],
        'bridge_update' => ['request_id'], 'bridge_update_status' => [],
    ];
    if (!isset($fields[$action])) ariaFail('unsupported action');
    if (array_diff(array_keys($a), $fields[$action])) ariaFail('invalid request');
    if (in_array('request_id', $fields[$action], true)) ariaName($a['request_id'] ?? null, 'id');
    if (in_array('name', $fields[$action], true)) ariaName($a['name'] ?? null, strpos($action, 'script_') === 0 ? 'script' : 'container');
    if (in_array('template', $fields[$action], true)) ariaName($a['template'] ?? null, 'template');
    if ($action === 'job_status') ariaName($a['job_id'] ?? null, 'id');
    if (in_array('expected_sha256', $fields[$action], true)) {
        if (!isset($a['expected_sha256']) || !is_string($a['expected_sha256']) || !preg_match($action === 'template_save' ? '/^(?:[a-f0-9]{64})?$/D' : '/^[a-f0-9]{64}$/D', $a['expected_sha256'])) ariaFail('invalid request');
    }
    if ($action === 'template_save') {
        if (!isset($a['xml']) || !is_string($a['xml'])) ariaFail('invalid request');
        ariaXML($a['xml']);
    }
    if ($action === 'container_action' && !in_array($a['action'] ?? null, ['start', 'stop', 'restart'], true)) ariaFail('invalid request');
    foreach (['pull', 'start'] as $key) if (in_array($key, $fields[$action], true)) {
        if (isset($a[$key]) && !is_bool($a[$key])) ariaFail('invalid request');
        $a[$key] = $a[$key] ?? true;
    }
    foreach (['tail' => [100, 500], 'timeout_seconds' => [3600, 86400]] as $key => $range) if (in_array($key, $fields[$action], true)) {
        $a[$key] = $a[$key] ?? $range[0];
        if (!is_int($a[$key]) || $a[$key] < 1 || $a[$key] > $range[1]) ariaFail('invalid request');
    }
    return $a;
}
function ariaCanonical(array $a): array {
    if ($a !== [] && array_keys($a) !== range(0, count($a)-1)) ksort($a);
    foreach ($a as $key => $value) if (is_array($value)) $a[$key] = ariaCanonical($value);
    return $a;
}
function ariaPublicJob(array $job): array { unset($job['arguments'], $job['fingerprint']); return $job; }
function ariaBridgeUpdateRunner(): string {
    return ariaPath('ARIA_AGENT_UPDATE_RUNNER', __DIR__.'/bridge-update.php');
}
function ariaBridgeUpdateAvailable(): bool {
    return is_file(ariaBridgeUpdateRunner()) && !is_link(ariaBridgeUpdateRunner());
}
function ariaBridgeLatest(string $state): ?array {
    $pointer = "$state/bridge-update-latest.json";
    if (!is_file($pointer)) return null;
    $latest = ariaReadJson($pointer);
    $id = ariaName($latest['job_id'] ?? null, 'id');
    $path = "$state/bridge-updates/$id.json";
    if (!is_file($path)) ariaFail('internal error');
    return ariaReadJson($path);
}
function ariaBridgePublic(array $job): array {
    unset($job['pid'], $job['start_time']);
    return $job;
}
function ariaBridgeBusy(string $state): bool {
    $latest = ariaBridgeLatest($state);
    return $latest !== null && in_array($latest['status'] ?? '', ['queued', 'running', 'unknown'], true);
}
function ariaBridgeStatus(string $state): array {
    $queueLock = ariaLock($state, 'queue');
    try {
        $job = ariaBridgeLatest($state);
        if ($job === null) return ['job_id' => null, 'request_id' => null, 'action' => 'bridge_update', 'status' => 'idle', 'phase' => 'idle', 'created_at' => null, 'started_at' => null, 'finished_at' => null, 'from_revision' => null, 'to_revision' => null, 'error' => null, 'result' => null];
        $stale = ($job['status'] === 'queued' && (strtotime($job['created_at']) ?: 0) < time() - 60) || $job['status'] === 'running';
        if ($stale) {
            $runnerLock = fopen("$state/bridge-update-runner.lock", 'ce');
            if ($runnerLock !== false) {
                if (flock($runnerLock, LOCK_EX | LOCK_NB)) {
                    // Re-read under the runner lock. It may have committed its
                    // terminal status between our first read and its release.
                    $path = "$state/bridge-updates/{$job['job_id']}.json";
                    $job = ariaReadJson($path);
                    $stillStale = ($job['status'] === 'queued' && (strtotime($job['created_at']) ?: 0) < time() - 60) || $job['status'] === 'running';
                    if ($stillStale) {
                        $job['status'] = 'unknown'; $job['phase'] = 'interrupted';
                        $job['error'] = 'bridge update interrupted'; $job['finished_at'] = ariaNow();
                        ariaAtomic($path, ariaJson($job));
                    }
                    flock($runnerLock, LOCK_UN);
                }
                fclose($runnerLock);
            }
        }
        return ariaBridgePublic($job);
    } finally { ariaUnlock($queueLock); }
}
function ariaBridgeQueue(string $state, array $a, ?string $targetRevision = null): array {
    if ($targetRevision !== null && !preg_match('/^[a-f0-9]{40}$/D', $targetRevision)) ariaFail('invalid request');
    $queueLock = ariaLock($state, 'queue');
    try {
        $requestId = $a['request_id'];
        $fingerprint = hash('sha256', ariaJson(['bridge_update', $targetRevision === null ? [] : ['target_revision' => $targetRevision]]));
        $receiptPath = "$state/requests/$requestId.json";
        if (is_file($receiptPath)) {
            $receipt = ariaReadJson($receiptPath);
            if (!hash_equals($receipt['fingerprint'], $fingerprint)) ariaFail('request_id conflict');
            $path = "$state/bridge-updates/{$receipt['job_id']}.json";
            $status = is_file($path) ? ariaReadJson($path)['status'] : 'unknown';
            return ['job_id' => $receipt['job_id'], 'status' => $status, 'deduplicated' => true];
        }
        if (ariaBridgeBusy($state)) ariaFail('management busy');
        foreach (glob("$state/jobs/*.json") ?: [] as $path) {
            if (in_array(ariaReadJson($path)['status'], ['queued', 'running'], true)) ariaFail('management busy');
        }
        if (!ariaBridgeUpdateAvailable()) ariaFail('bridge update unavailable');
        if (count(glob("$state/requests/*.json") ?: []) >= ARIA_MAX_RECEIPTS) ariaFail('deduplication capacity reached');
        $jobId = gmdate('YmdHis').'-'.bin2hex(random_bytes(12));
        $job = ['job_id' => $jobId, 'request_id' => $requestId, 'action' => 'bridge_update', 'status' => 'queued', 'phase' => 'queued', 'created_at' => ariaNow(), 'started_at' => null, 'finished_at' => null, 'from_revision' => null, 'to_revision' => null, 'error' => null, 'result' => null];
        if ($targetRevision !== null) $job['target_revision'] = $targetRevision;
        // Receipt first means even a failed or interrupted launch cannot be
        // replayed by retrying a request whose acknowledgement was lost.
        ariaAtomic($receiptPath, ariaJson(['fingerprint' => $fingerprint, 'job_id' => $jobId, 'action' => 'bridge_update']));
        ariaAtomic("$state/bridge-updates/$jobId.json", ariaJson($job));
        ariaAtomic("$state/bridge-update-latest.json", ariaJson(['job_id' => $jobId]));
        $launched = false;
        try {
            $pipes = [];
            if (!is_dir('/proc/self/fd')) ariaFail('bridge update unavailable');
            $descriptors = [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'a'], 2 => ['file', '/dev/null', 'a']];
            // PHP's listening/accepted Unix streams do not universally set
            // CLOEXEC. Remap every other inherited descriptor before exec so
            // the updater cannot retain the old listener, client, or locks.
            foreach (glob('/proc/self/fd/*') ?: [] as $descriptorPath) {
                $descriptor = basename($descriptorPath);
                if (ctype_digit($descriptor) && (int)$descriptor >= 3) $descriptors[(int)$descriptor] = ['file', '/dev/null', 'r+'];
            }
            // No ariaRun here: its timeout process group is intentionally
            // cleaned up with the host worker. This runner must outlive both
            // host services and the bridge it is replacing.
            $process = proc_open([
                ariaPath('ARIA_AGENT_SETSID', '/usr/bin/setsid'), '--fork',
                '/usr/bin/php', ariaBridgeUpdateRunner(), $state, $jobId,
            ], $descriptors, $pipes, __DIR__, null, ['bypass_shell' => true]);
            if (!is_resource($process)) ariaFail('bridge update unavailable');
            $launched = true;
            if (proc_close($process) !== 0) ariaFail('bridge update unavailable');
        } catch (Throwable $e) {
            $runnerLock = fopen("$state/bridge-update-runner.lock", 'ce');
            if ($runnerLock !== false) {
                if (flock($runnerLock, LOCK_EX | LOCK_NB)) {
                    $job = ariaReadJson("$state/bridge-updates/$jobId.json");
                    if (in_array($job['status'], ['queued', 'running'], true)) {
                        // A setsid launcher can fail after it forked. Never
                        // unblock mutations based on an ambiguous launcher exit.
                        $job['status'] = $launched ? 'unknown' : 'failed';
                        $job['phase'] = $launched ? 'launch_uncertain' : 'launch_failed';
                        $job['error'] = 'bridge update unavailable'; $job['finished_at'] = ariaNow();
                        ariaAtomic("$state/bridge-updates/$jobId.json", ariaJson($job));
                    }
                    flock($runnerLock, LOCK_UN);
                }
                fclose($runnerLock);
            }
            $job = ariaReadJson("$state/bridge-updates/$jobId.json");
            return ['job_id' => $jobId, 'status' => $job['status'], 'deduplicated' => false];
        }
        return ['job_id' => $jobId, 'status' => 'queued', 'deduplicated' => false];
    } finally { ariaUnlock($queueLock); }
}
function ariaQueue(string $state, string $action, array $a): array {
    $lock = ariaLock($state, 'queue');
    try {
        $id = $a['request_id']; unset($a['request_id']);
        $fingerprint = hash('sha256', ariaJson([$action, ariaCanonical($a)]));
        $receiptPath = "$state/requests/$id.json";
        if (file_exists($receiptPath)) {
            $receipt = ariaReadJson($receiptPath);
            if (!hash_equals($receipt['fingerprint'], $fingerprint)) ariaFail('request_id conflict');
            $jobFile = "$state/jobs/{$receipt['job_id']}.json";
            $status = is_file($jobFile) ? ariaReadJson($jobFile)['status'] : 'expired';
            return ['job_id' => $receipt['job_id'], 'status' => $status, 'deduplicated' => true];
        }
        if (ariaBridgeBusy($state)) ariaFail('management busy');
        ariaExtensionAuthorize($state, $action, $a);
        if (count(glob("$state/requests/*.json") ?: []) >= ARIA_MAX_RECEIPTS) ariaFail('deduplication capacity reached');
        $pending = 0;
        foreach (glob("$state/jobs/*.json") ?: [] as $path) if (in_array(ariaReadJson($path)['status'], ['queued', 'running'], true)) $pending++;
        if ($pending >= ARIA_MAX_PENDING) ariaFail('queue full');
        // Capture hashes now, not when a delayed bulk job finally begins.
        if ($action === 'container_update') {
            if (ariaProtected($a['name'])) ariaFail('protected container');
            $a['plan'] = ariaFindTemplate($a['name']);
        }
        if ($action === 'containers_update_all') {
            $a['plan'] = [];
            foreach (ariaContainers() as $row) {
                $name = ariaName($row['Names'] ?? null, 'container');
                if (ariaProtected($name, true)) { $a['plan'][] = ['name' => $name, 'skip' => 'protected container']; continue; }
                try { $a['plan'][] = ariaFindTemplate($name); }
                catch (Throwable $e) { $a['plan'][] = ['name' => $name, 'skip' => 'no unique valid template']; }
            }
        }
        $jobId = gmdate('YmdHis').'-'.bin2hex(random_bytes(12));
        $job = ['job_id' => $jobId, 'action' => $action, 'status' => 'queued', 'created_at' => ariaNow(), 'started_at' => null, 'finished_at' => null, 'arguments' => $a, 'result' => null, 'error' => null];
        if (isset($GLOBALS['ARIA_AUTOMATION_CONTEXT'])) $job['automation'] = $GLOBALS['ARIA_AUTOMATION_CONTEXT'];
        // Persist the receipt first: a crash can make this request unknown, never replay it.
        ariaAtomic($receiptPath, ariaJson(['fingerprint' => $fingerprint, 'job_id' => $jobId]));
        ariaAtomic("$state/jobs/$jobId.json", ariaJson($job));
        return ['job_id' => $jobId, 'status' => 'queued', 'deduplicated' => false];
    } finally { ariaUnlock($lock); }
}
function ariaDispatch(string $state, $request): array {
    if (!is_array($request) || array_diff(array_keys($request), ['action', 'arguments']) || !is_string($request['action'] ?? null) || !isset($request['arguments']) || !is_array($request['arguments'])) ariaFail('invalid request');
    $action = $request['action']; $a = ariaValidate($action, $request['arguments']);
    if ($action === 'bridge_update') return ariaBridgeQueue($state, $a);
    if ($action === 'bridge_update_status') return ariaBridgeStatus($state);
    if ($action === 'automation_job_cancel' && function_exists('ariaAutomationControl')) return ariaAutomationControl($state, $action, $a);
    if (isset($a['request_id'])) return ariaQueue($state, $action, $a);
    ariaExtensionAuthorize($state, $action, $a);
    if (isset(ariaExtensionSpecs()[$action])) return ariaExtensionRead($state, $action, $a);
    switch ($action) {
        case 'capabilities':
            return ['version' => 2, 'bridge_version' => '2.0.0', 'extension_actions' => array_keys(ariaExtensionSpecs()), 'host_execution' => true, 'queue' => true, 'requires_request_id' => true, 'bridge_self_update' => ariaBridgeUpdateAvailable(), 'bridge_update_status_action' => 'bridge_update_status', 'native_converter_available' => is_file(ariaPath('ARIA_AGENT_NATIVE', '/usr/local/emhttp/plugins/dynamix.docker.manager/include/DockerClient.php')), 'bulk_skipped_containers' => ARIA_PROTECTED, 'protected_containers' => ['aria-gpt-bridge'], 'script_scope' => 'installed Unraid User Scripts, root on host', 'template_secrets' => 'environment values, labels, and sensitive fields redacted; keep placeholders when editing', 'output_redaction' => 'best effort; scripts and application logs can print secrets in unrecognized formats', 'unsupported_template_features' => ['native Tailscale', 'replacement with Docker volumes not explicitly tracked in the template'], 'max_template_bytes' => ARIA_MAX_XML, 'max_script_bytes' => ARIA_MAX_XML, 'max_timeout_seconds' => 86400, 'job_retention' => ARIA_MAX_JOBS, 'deduplication_receipt_limit' => ARIA_MAX_RECEIPTS];
        case 'containers': return ['containers' => ariaContainers(), 'checked_at' => ariaNow()];
        case 'container_inspect':
            $info = ariaInspect($a['name']); $env = [];
            foreach ($info['Config']['Env'] ?? [] as $entry) $env[explode('=', $entry, 2)[0]] = '[REDACTED]';
            return ['name' => ltrim($info['Name'] ?? $a['name'], '/'), 'id' => $info['Id'], 'image' => $info['Config']['Image'] ?? null, 'image_id' => $info['Image'] ?? null, 'state' => array_intersect_key($info['State'] ?? [], array_flip(['Status', 'Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead', 'Pid', 'ExitCode', 'StartedAt', 'FinishedAt'])), 'environment' => $env, 'mounts' => $info['Mounts'] ?? [], 'ports' => $info['NetworkSettings']['Ports'] ?? [], 'networks' => array_keys($info['NetworkSettings']['Networks'] ?? []), 'restart_policy' => $info['HostConfig']['RestartPolicy'] ?? [], 'privileged' => $info['HostConfig']['Privileged'] ?? false, 'redacted' => true];
        case 'container_logs':
            $info = ariaInspect($a['name']); $secrets = [];
            foreach ($info['Config']['Env'] ?? [] as $entry) $secrets[] = explode('=', $entry, 2)[1] ?? '';
            $r = ariaDocker(['logs', '--tail', (string)$a['tail'], '--timestamps', $a['name']]);
            return ['name' => $a['name'], 'output' => ariaOpsScrub($state, ariaScrub($r['output'], $secrets)), 'truncated' => $r['truncated'], 'redacted' => true];
        case 'templates_list': return ['templates' => ariaTemplateList()];
        case 'template_get':
            $xml = ariaReadFile(ariaTemplatePath($a['template']), ARIA_MAX_XML);
            return ['template' => $a['template'], 'sha256' => hash('sha256', $xml), 'xml' => ariaRedactedXML($xml)[0], 'redacted' => true];
        case 'scripts_list':
            $rows = [];
            foreach (glob(ariaScripts().'/*', GLOB_ONLYDIR) ?: [] as $dir) {
                try { $path = ariaScriptPath(basename($dir)); $rows[] = ['name' => basename($dir), 'sha256' => hash_file('sha256', $path)]; } catch (Throwable $e) { }
            }
            return ['scripts' => $rows];
        case 'script_get':
            $script = ariaReadFile(ariaScriptPath($a['name']), ARIA_MAX_XML);
            return ['name' => $a['name'], 'sha256' => hash('sha256', $script), 'script' => ariaOpsScrub($state, ariaScrub($script, [], ARIA_MAX_XML), ARIA_MAX_XML), 'redacted' => true, 'truncated' => false];
        case 'job_status':
            $path = "$state/jobs/{$a['job_id']}.json";
            if (!is_file($path)) ariaFail('job unknown');
            return ariaPublicJob(ariaReadJson($path));
    }
    ariaFail('unsupported action');
}

function ariaNativeCommand(string $xml): array {
    global $docroot, $var, $driver, $dockerManPaths, $dockercfg, $custom, $subnet;
    $file = ariaPath('ARIA_AGENT_NATIVE', '/usr/local/emhttp/plugins/dynamix.docker.manager/include/DockerClient.php');
    if (!is_file($file)) ariaFail('native template converter unavailable');
    $docroot = '/usr/local/emhttp';
    $_SERVER['DOCUMENT_ROOT'] = $docroot; $_SERVER['REQUEST_URI'] = 'docker';
    ob_start();
    try {
        if (!ariaTest()) require_once "$docroot/webGui/include/Helpers.php";
        require_once $file;
        if (!function_exists('xmlToCommand')) ariaFail('native template converter unavailable');
        $var = ariaTest() ? ['timeZone' => 'UTC', 'NAME' => 'test'] : @parse_ini_file('/var/local/emhttp/var.ini');
        if (!is_array($var)) ariaFail('native template converter unavailable');
        $driver = class_exists('DockerUtil') ? DockerUtil::driver() : [];
        $custom = class_exists('DockerUtil') ? DockerUtil::custom() : [];
        $subnet = class_exists('DockerUtil') ? DockerUtil::network($custom) : [];
        if (function_exists('xmlToVar')) {
            $resolved = xmlToVar($xml); $tree = ariaXML($xml);
            $override = function_exists('hasNetworkParam') && hasNetworkParam((string)$tree->ExtraParams);
            if (!$override && strtolower($resolved['Network'] ?? '') !== strtolower(ariaRequestedNetwork($tree))) ariaFail('unsupported template feature');
        }
        $parts = xmlToCommand($xml, true);
    } finally { ob_end_clean(); }
    if (!is_array($parts) || count($parts) < 3 || !is_string($parts[0]) || !preg_match('~^/[^\r\n ]*/docker\s+create\s~', $parts[0])) ariaFail('native template converter unavailable');
    return $parts;
}
function ariaDeployment(string $state, string $jobId, array $a, bool $updating = false): array {
    $path = ariaTemplatePath($a['template']); ariaHash($path, $a['expected_sha256']);
    $xml = ariaReadFile($path, ARIA_MAX_XML);
    if (!hash_equals($a['expected_sha256'], hash('sha256', $xml))) ariaFail('hash mismatch');
    $tree = ariaXML($xml); $name = (string)$tree->Name;
    ariaExtensionAuthorize($state, $updating ? 'container_update' : 'template_deploy', array_merge($a, ['name' => $name]));
    if (ariaProtected($name)) ariaFail('protected container');
    if (strtolower((string)$tree->TailscaleEnabled) === 'true') ariaFail('unsupported template feature');
    [$command, $nativeName, $image] = ariaNativeCommand($xml);
    if ($nativeName !== $name || $image !== (string)$tree->Repository) ariaFail('invalid template');
    $old = ariaInspect($name, false);
    if ($updating && $old === null) ariaFail('not found');
    // A VOLUME declaration creates a new anonymous volume during Docker create.
    // Reject untracked volumes instead of silently starting with empty data.
    foreach ($old['Mounts'] ?? [] as $mount) {
        if (($mount['Type'] ?? '') !== 'volume') continue;
        $tracked = false;
        foreach ($tree->Config as $config) {
            if (strtolower((string)$config['Type']) !== 'path') continue;
            $source = (string)$config !== '' ? (string)$config : (string)$config['Default'];
            if ((string)$config['Target'] === ($mount['Destination'] ?? '') && $source === ($mount['Name'] ?? '')) $tracked = true;
        }
        foreach ($tree->Data->Volume ?? [] as $volume) {
            if ((string)$volume->ContainerDir === ($mount['Destination'] ?? '') && (string)$volume->HostDir === ($mount['Name'] ?? '')) $tracked = true;
        }
        if (!$tracked) ariaFail('unsupported template feature');
    }
    $extraNetworks = []; $seenNetworks = [strtolower(ariaRequestedNetwork($tree)) => true];
    foreach (preg_split('/[\s,]+/', trim((string)$tree->ExtraNetworks), -1, PREG_SPLIT_NO_EMPTY) as $network) {
        if (isset($seenNetworks[strtolower($network)])) continue;
        $seenNetworks[strtolower($network)] = true; $extraNetworks[] = $network;
    }
    // No changes to the old container until the image and native conversion succeed.
    if ($a['pull']) ariaDocker(['pull', $image], 3600);
    ariaHash($path, $a['expected_sha256']);
    $current = ariaInspect($name, false);
    if (($old['Id'] ?? null) !== ($current['Id'] ?? null)) ariaFail('hash mismatch');
    $old = $current;
    $start = $updating ? !empty($old['State']['Running']) : $a['start'];
    $backup = 'aria-backup-'.substr($name, 0, 60).'-'.substr(hash('sha256', $jobId.$name), 0, 16);
    $oldId = $old['Id'] ?? null; $createdId = null;
    if ($oldId !== null && (!is_string($oldId) || !preg_match('/^[a-f0-9]{64}$/D', $oldId))) ariaFail('command failed');
    $wasRunning = !empty($old['State']['Running']); $renamed = false;
    if ($old !== null && ariaInspect($backup, false) !== null) ariaFail('command failed');
    $secrets = array_values(ariaRedactedXML($xml)[1]);
    $assertOwner = function(string $id, string $expectedName): array {
        $info = ariaInspect($id);
        if (($info['Id'] ?? null) !== $id || ($info['Name'] ?? null) !== '/'.$expectedName) ariaFail('hash mismatch');
        return $info;
    };
    try {
        if ($oldId !== null) {
            $assertOwner($oldId, $name);
            if ($wasRunning) ariaDocker(['stop', '--time', '30', $oldId], 45);
            $assertOwner($oldId, $name);
            ariaDocker(['rename', $oldId, $backup]); $renamed = true;
            $assertOwner($oldId, $backup);
        }
        // Only Unraid's trusted converter generates shell syntax. The API accepts
        // no command string; template ExtraParams/PostArgs intentionally carry
        // the same host privilege as editing a template in Unraid itself.
        $r = ariaRun(['/bin/bash', '-c', $command], 600);
        // Ownership comes from the immutable ID emitted by our own create, never
        // from whichever container happens to occupy the requested name later.
        preg_match_all('/(?:^|\n)([a-f0-9]{64})(?=\r?\n|\r?$)/D', $r['output'], $emitted);
        $ids = array_values(array_unique($emitted[1]));
        if (count($ids) === 1 && $ids[0] !== $oldId) $createdId = $ids[0];
        if ($r['exit_code'] !== 0 || $createdId === null) ariaFail('command failed');
        $assertOwner($createdId, $name);
        foreach ($extraNetworks as $network) {
            if ($network === (string)$tree->Network) continue;
            $assertOwner($createdId, $name);
            ariaDocker(['network', 'connect', $network, $createdId]);
        }
        $configured = $assertOwner($createdId, $name);
        $networkOverride = function_exists('hasNetworkParam') && hasNetworkParam((string)$tree->ExtraParams);
        if (!$networkOverride) {
            $expectedNetwork = strtolower(ariaRequestedNetwork($tree));
            if (strpos($expectedNetwork, 'container:') === 0) {
                $networkOwner = ariaInspect(substr(ariaRequestedNetwork($tree), 10));
                $expectedNetwork = 'container:'.$networkOwner['Id'];
            }
            if (strtolower($configured['HostConfig']['NetworkMode'] ?? '') !== $expectedNetwork) ariaFail('command failed');
        }
        $health = ['application_health_verified' => false];
        if ($start) {
            $assertOwner($createdId, $name);
            ariaDocker(['start', $createdId], 120);
            $assertOwner($createdId, $name);
            if (function_exists('addRoute')) { ob_start(); try { addRoute($name); } finally { ob_end_clean(); } }
            $check = $assertOwner($createdId, $name);
            if (empty($check['State']['Running'])) ariaFail('command failed');
            if (function_exists('ariaDeploymentHealth')) $health = ariaDeploymentHealth($state, $name);
        }
        $assertOwner($createdId, $name);
        // Existing user data remains attached to the replacement; never -v.
        $retained = false; $backupRestartDisabled = null; $backupCleanupError = null;
        if ($renamed) {
            // Once cleanup can remove the original, an uncertain cleanup result
            // must not trigger removal of the healthy replacement as rollback.
            try {
                $assertOwner($oldId, $backup);
                $remove = ariaDocker(['rm', $oldId], 30, false);
                $retained = $remove['exit_code'] !== 0 && ariaInspect($oldId, false) !== null;
                if ($retained) {
                    $assertOwner($oldId, $backup);
                    $backupRestartDisabled = ariaDocker(['update', '--restart=no', $oldId], 30, false)['exit_code'] === 0;
                }
            } catch (Throwable $cleanupError) { $retained = null; $backupCleanupError = 'backup cleanup could not be verified'; }
        }
        return ['name' => $name, 'container_id' => $createdId, 'template' => $a['template'], 'image' => $image, 'status' => $start ? 'running' : 'created', 'pulled' => $a['pull'], 'backup_retained' => $retained, 'backup_name' => $retained !== false ? $backup : null, 'backup_id' => $retained !== false ? $oldId : null, 'backup_restart_disabled' => $backupRestartDisabled, 'backup_cleanup_error' => $backupCleanupError, 'output' => ariaScrub($r['output'], $secrets), 'truncated' => $r['truncated'], 'application_health_verified' => $health['application_health_verified'] ?? false, 'health' => $health];
    } catch (Throwable $e) {
        $rollbackOk = true;
        try {
            if ($createdId !== null && ariaInspect($createdId, false) !== null) {
                $removed = ariaDocker(['rm', '--force', $createdId], 45, false);
                if ($removed['exit_code'] !== 0) ariaFail('rollback failed');
            }
            if ($oldId !== null) {
                $original = ariaInspect($oldId, false);
                $occupant = ariaInspect($name, false);
                if ($original === null || ($original['Id'] ?? null) !== $oldId) ariaFail('rollback failed');
                // This also recovers a rename whose acknowledgement was lost.
                // Never rename an externally moved original or replace a foreign
                // container now occupying the original name.
                if (($original['Name'] ?? null) === '/'.$backup && $occupant === null) {
                    $restored = ariaDocker(['rename', $oldId, $name], 30, false);
                    if ($restored['exit_code'] !== 0) ariaFail('rollback failed');
                } elseif (($original['Name'] ?? null) !== '/'.$name || ($occupant['Id'] ?? null) !== $oldId) ariaFail('rollback failed');
                $assertOwner($oldId, $name);
                if ($wasRunning) {
                    $restarted = ariaDocker(['start', $oldId], 120, false);
                    if ($restarted['exit_code'] !== 0) ariaFail('rollback failed');
                    $restored = $assertOwner($oldId, $name);
                    if (empty($restored['State']['Running'])) ariaFail('rollback failed');
                }
            }
        } catch (Throwable $recoveryError) { $rollbackOk = false; }
        if (!$rollbackOk) ariaFail('rollback failed');
        throw $e;
    }
}
function ariaExecute(string $state, array $job): array {
    $a = $job['arguments']; $id = $job['job_id'];
    ariaExtensionAuthorize($state, $job['action'], $a);
    if (isset(ariaExtensionSpecs()[$job['action']])) return ariaExtensionExecute($state, $job);
    switch ($job['action']) {
        case 'container_action':
            if (ariaProtected($a['name'])) ariaFail('protected container');
            ariaInspect($a['name']);
            ariaDocker([$a['action'], $a['name']], 120);
            $info = ariaInspect($a['name']);
            if ((!empty($info['State']['Running'])) !== ($a['action'] !== 'stop')) ariaFail('command failed');
            return ['name' => $a['name'], 'action' => $a['action'], 'status' => $info['State']['Status'] ?? 'unknown'];
        case 'template_save':
            $path = ariaTemplatePath($a['template'], false); $old = null;
            if (file_exists($path)) {
                if ($a['expected_sha256'] === '') ariaFail('template exists');
                $old = ariaReadFile($path, ARIA_MAX_XML);
                if (!hash_equals($a['expected_sha256'], hash('sha256', $old))) ariaFail('hash mismatch');
            } elseif ($a['expected_sha256'] !== '') ariaFail('hash mismatch');
            $xml = ariaRestoreXML($a['xml'], $old);
            if (ariaProtected((string)ariaXML($xml)->Name) || ($old !== null && ariaProtected((string)ariaXML($old)->Name))) ariaFail('protected container');
            if ($old !== null) ariaAtomic("$state/backups/$id.xml", $old);
            // Recheck after serialization and backup, before atomic replacement.
            if ($old !== null) ariaHash($path, $a['expected_sha256']);
            elseif (file_exists($path)) ariaFail('template exists');
            ariaAtomic($path, $xml);
            return ['template' => $a['template'], 'sha256' => hash('sha256', $xml), 'backup_created' => $old !== null, 'deployed' => false];
        case 'template_deploy': return ariaDeployment($state, $id, $a);
        case 'script_run':
            $path = ariaScriptPath($a['name']);
            // Execute exactly the reviewed bytes; the working directory stays the
            // installed script directory for User Scripts' relative files.
            $script = ariaReadFile($path, ARIA_MAX_XML);
            if (!hash_equals($a['expected_sha256'], hash('sha256', $script))) ariaFail('hash mismatch');
            $copy = "$state/jobs/$id.script"; ariaAtomic($copy, $script, 0700);
            try { $r = ariaRun(['/bin/bash', $copy], $a['timeout_seconds'], dirname($path)); }
            finally { @unlink($copy); }
            $r['name'] = $a['name']; $r['output'] = ariaOpsScrub($state, $r['output']);
            return $r;
        case 'container_update':
            $plan = $a['plan'];
            return ariaDeployment($state, $id, ['template' => $plan['template'], 'expected_sha256' => $plan['sha256'], 'pull' => true, 'start' => true], true);
        case 'containers_update_all':
            $results = [];
            foreach ($a['plan'] as $plan) {
                if (isset($plan['skip'])) { $results[] = ['name' => $plan['name'], 'status' => 'skipped', 'reason' => $plan['skip']]; continue; }
                try {
                    $r = ariaDeployment($state, $id, ['template' => $plan['template'], 'expected_sha256' => $plan['sha256'], 'pull' => true, 'start' => true], true);
                    unset($r['output']); $r['status'] = 'updated'; $results[] = $r;
                } catch (Throwable $e) { $results[] = ['name' => $plan['name'], 'status' => 'failed', 'error' => ariaError($e)]; }
            }
            return ['containers' => $results, 'all_updated' => count(array_filter($results, function ($r) { return $r['status'] !== 'updated'; })) === 0];
    }
    ariaFail('unsupported action');
}
function ariaError(Throwable $e): string { return in_array($e->getMessage(), ARIA_ERRORS, true) ? $e->getMessage() : 'internal error'; }
function ariaPrune(string $state): void {
    $files = glob("$state/jobs/*.json") ?: []; sort($files);
    foreach ($files as $path) {
        if (count($files) <= ARIA_MAX_JOBS) break;
        $job = ariaReadJson($path);
        if (in_array($job['status'], ['queued', 'running'], true)) continue;
        @unlink("$state/backups/{$job['job_id']}.xml"); @unlink("$state/backups/{$job['job_id']}.meta.json"); @unlink($path); array_pop($files);
    }
}
function ariaWorker(string $state): void {
    $worker = ariaLock($state, 'worker', true);
    $GLOBALS['ARIA_WORKER_STATE'] = $state;
    ariaAtomic($state.'/worker-ready.json', ariaJson(['pid' => getmypid(), 'ready_at' => ariaNow()]));
    // A running job may have performed its effect before the process was lost.
    // Mark unknown, never restart it. Request receipts remain deduplicated.
    foreach (glob("$state/jobs/*.json") ?: [] as $path) {
        $job = ariaReadJson($path);
        if ($job['status'] === 'running') {
            $job['status'] = 'unknown'; $job['error'] = 'worker interrupted; inspect host before retrying with a new request_id'; $job['finished_at'] = ariaNow();
            ariaAtomic($path, ariaJson($job));
        }
        @unlink(substr($path, 0, -5).'.script');
    }
    while (true) {
        $found = false;
        foreach (glob("$state/jobs/*.json") ?: [] as $path) {
            $claim = ariaLock($state, 'queue');
            try {
                if (!is_file($path)) continue;
                $job = ariaReadJson($path);
                if ($job['status'] !== 'queued') continue;
                $found = true; $job['status'] = 'running'; $job['started_at'] = ariaNow();
                ariaAtomic($path, ariaJson($job));
            } finally { ariaUnlock($claim); }
            $GLOBALS['ARIA_CURRENT_JOB_ID'] = $job['job_id'];
            try {
                $job['result'] = ariaExecute($state, $job);
                $job['status'] = isset($job['result']['exit_code']) && $job['result']['exit_code'] !== 0 ? 'failed' : 'succeeded';
                if ($job['status'] === 'failed') $job['error'] = !empty($job['result']['timed_out']) ? 'operation timed out' : 'command failed';
                if ($job['action'] === 'containers_update_all' && !$job['result']['all_updated']) $job['status'] = 'partial';
                if (in_array($job['result']['status'] ?? '', ['failed', 'unknown'], true)) { $job['status'] = $job['result']['status']; $job['error'] = $job['status'] === 'unknown' ? 'operation outcome unknown' : 'command failed'; }
                if (($job['result']['partial'] ?? false) === true || ($job['result']['status'] ?? '') === 'partial') $job['status'] = 'partial';
            } catch (Throwable $e) { $job['error'] = ariaError($e); $job['status'] = $job['error'] === 'application outcome unknown' ? 'unknown' : 'failed'; }
            unset($GLOBALS['ARIA_CURRENT_JOB_ID']);
            $lastRecord = ariaReadJson($path);
            if (isset($lastRecord['progress'])) $job['progress'] = $lastRecord['progress'];
            $job['finished_at'] = ariaNow(); unset($job['arguments']);
            ariaAtomic($path, ariaJson($job));
            ariaPrune($state);
        }
        if (!$found) usleep(250000);
    }
    ariaUnlock($worker);
}
function ariaServe(string $state, string $socket): void {
    $GLOBALS['ARIA_WORKER_STATE'] = $state;
    $GLOBALS['ARIA_PROCESS_RECORD'] = 'active-serve-process.json';
    $serverLock = ariaLock($state, 'server', true);
    $dir = dirname($socket);
    if (is_link($dir) || is_link($socket)) ariaFail('internal error');
    if (!is_dir($dir) && !mkdir($dir, 0750, true)) ariaFail('internal error');
    if (!chmod($dir, 0750)) ariaFail('internal error');
    if (!ariaTest() && (!chown($dir, 0) || !chgrp($dir, 65532))) ariaFail('internal error');
    if (file_exists($socket)) {
        if (filetype($socket) !== 'socket') ariaFail('internal error');
        unlink($socket);
    }
    $server = stream_socket_server('unix://'.$socket, $errno, $error, STREAM_SERVER_BIND | STREAM_SERVER_LISTEN);
    if ($server === false) ariaFail('internal error');
    if (!chmod($socket, 0660)) ariaFail('internal error');
    if (!ariaTest() && (!chown($socket, 0) || !chgrp($socket, 65532))) ariaFail('internal error');
    while ($client = @stream_socket_accept($server, -1)) {
        stream_set_timeout($client, 5);
        try {
            $line = fgets($client, ARIA_MAX_INPUT + 2);
            if ($line === false || strlen($line) > ARIA_MAX_INPUT || substr($line, -1) !== "\n") ariaFail('invalid request');
            $decoded = json_decode($line, true);
            if (json_last_error() !== JSON_ERROR_NONE) ariaFail('invalid request');
            $reply = ['ok' => true, 'result' => ariaDispatch($state, $decoded)];
        } catch (Throwable $e) { $reply = ['ok' => false, 'error' => ariaError($e)]; }
        $out = ariaJson($reply)."\n";
        if (strlen($out) > ARIA_MAX_INPUT) $out = ariaJson(['ok' => false, 'error' => 'command failed'])."\n";
        $offset = 0;
        while ($offset < strlen($out)) { $n = @fwrite($client, substr($out, $offset)); if (!$n) break; $offset += $n; }
        fclose($client);
    }
    ariaUnlock($serverLock);
}
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    ini_set('display_errors', '0');
    try {
        if (PHP_VERSION_ID < 70400 || !function_exists('simplexml_load_string') || !function_exists('proc_open') || !function_exists('posix_kill') || count($argv) < 3) ariaFail('internal error');
        $mode = $argv[1]; $state = rtrim($argv[2], '/');
        if ($state === '' || $state[0] !== '/') ariaFail('invalid request');
        ariaInit($state);
        if ($mode === 'serve' && count($argv) === 4 && $argv[3][0] === '/') ariaServe($state, $argv[3]);
        elseif ($mode === 'work' && count($argv) === 3) ariaWorker($state);
        else ariaFail('invalid request');
    } catch (Throwable $e) { fwrite(STDERR, ariaError($e)."\n"); exit(1); }
}
