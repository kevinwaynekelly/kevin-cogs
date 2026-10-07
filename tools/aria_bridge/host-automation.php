<?php
/** Local, durable scheduling. Effects always pass through the ordinary host dispatcher. */
declare(strict_types=1);

const ARIA_AUTOMATION_ACTIONS = [
    'script_run', 'container_action', 'container_update', 'template_deploy',
    'operations_archive_create', 'operations_archive_verify', 'operations_script_run_arguments', 'operations_config_inventory',
    'applications_command',
];
function ariaAutomationDirectory(string $state): string {
    $dir = "$state/automation";
    if (is_link($dir)) ariaFail('internal error');
    if (!is_dir($dir) && !mkdir($dir, 0700, true)) ariaFail('internal error');
    chmod($dir, 0700);
    return $dir;
}
function ariaAutomationClock(): int { return ariaTest() && getenv('ARIA_AUTOMATION_TIME') !== false ? (int)getenv('ARIA_AUTOMATION_TIME') : time(); }
function ariaAutomationLoad(string $state, string $name, array $default = []): array {
    $path = ariaAutomationDirectory($state)."/$name.json";
    if (is_link($path)) ariaFail('internal error');
    return is_file($path) ? ariaReadJson($path) : $default;
}
function ariaAutomationSave(string $state, string $name, array $value): void { ariaAtomic(ariaAutomationDirectory($state)."/$name.json", ariaJson($value)); }
function ariaAutomationInit(string $state): void {
    ariaAutomationDirectory($state);
    $initLock = ariaLock($state, 'automation-init');
    try {
    if (!is_file("$state/automation/schedules.json")) {
        $now = ariaAutomationClock();
        $default = ['id' => 'bridge-daily-update', 'label' => 'Daily tested bridge update', 'enabled' => true,
            'trigger' => 'interval', 'interval_minutes' => 1440, 'daily_time' => '03:30', 'time_zone' => 'America/Chicago',
            'event_action' => '', 'event_status' => 'succeeded', 'action' => 'bridge_auto_update', 'arguments' => [],
            'window_start' => '02:00', 'window_end' => '06:00', 'window_timezone' => 'America/Chicago',
            'created_at' => gmdate('c', $now), 'updated_at' => gmdate('c', $now), 'generation' => bin2hex(random_bytes(8)),
            'not_before' => $now + 86400];
        ariaAutomationSave($state, 'schedules', ['version' => 1, 'schedules' => [$default['id'] => $default]]);
    }
    } finally { ariaUnlock($initLock); }
}
function ariaAutomationHash(array $schedule): string { return hash('sha256', ariaJson($schedule)); }
function ariaAutomationPublic(array $schedule): array {
    $public = $schedule; $public['sha256'] = ariaAutomationHash($schedule);
    // Scheduled arguments can include credentials. Return only their field names.
    $public['argument_fields'] = array_keys($schedule['arguments']); unset($public['arguments']);
    return $public;
}
function ariaAutomationNotice(string $state, string $id, string $kind, string $message, array $metadata = []): void {
    $noticeLock = ariaLock($state, 'automation-inbox');
    try {
    $index = ariaAutomationLoad($state, 'notice-index');
    if (isset($index[$id])) return;
    $inbox = ariaAutomationLoad($state, 'inbox');
    foreach ($inbox as $item) if ($item['id'] === $id) return;
    $inbox[] = ['id' => $id, 'kind' => $kind, 'message' => ariaScrub($message, [], 1024), 'metadata' => $metadata, 'created_at' => ariaNow(), 'acknowledged' => false];
    ariaAutomationSave($state, 'inbox', array_slice($inbox, -200));
    $index[$id] = true;
    ariaAutomationSave($state, 'notice-index', array_slice($index, -1024, null, true));
    } finally { ariaUnlock($noticeLock); }
}
function ariaAutomationValidate(string $action, array $a): array {
    $fields = [
        'automation_status' => [], 'automation_schedules' => [], 'automation_dashboard' => [],
        'automation_jobs' => ['status', 'limit'], 'automation_inbox' => ['include_acknowledged'],
        'automation_schedule_save' => ['id', 'label', 'enabled', 'trigger', 'interval_minutes', 'daily_time', 'time_zone', 'event_action', 'event_status', 'use_event_archive', 'action', 'arguments', 'window_start', 'window_end', 'window_timezone', 'expected_sha256', 'request_id'],
        'automation_schedule_delete' => ['id', 'expected_sha256', 'request_id'],
        'automation_job_cancel' => ['job_id', 'request_id'], 'automation_inbox_ack' => ['id', 'request_id'],
        'automation_bridge_check' => ['request_id'],
    ];
    if (!isset($fields[$action])) ariaFail('unsupported action');
    if (array_diff(array_keys($a), $fields[$action])) ariaFail('invalid request');
    foreach (['id', 'request_id', 'job_id'] as $field) if (in_array($field, $fields[$action], true)) ariaName($a[$field] ?? null, 'id');
    if (in_array('expected_sha256', $fields[$action], true) && (!is_string($a['expected_sha256'] ?? null) || !preg_match('/^(?:[a-f0-9]{64})?$/D', $a['expected_sha256']))) ariaFail('invalid request');
    if ($action === 'automation_jobs') {
        $a['status'] = $a['status'] ?? 'all'; $a['limit'] = $a['limit'] ?? 50;
        if (!in_array($a['status'], ['all', 'queued', 'running', 'succeeded', 'failed', 'partial', 'unknown', 'cancelled'], true) || !is_int($a['limit']) || $a['limit'] < 1 || $a['limit'] > 100) ariaFail('invalid request');
    }
    if ($action === 'automation_inbox') {
        $a['include_acknowledged'] = $a['include_acknowledged'] ?? false;
        if (!is_bool($a['include_acknowledged'])) ariaFail('invalid request');
    }
    if ($action === 'automation_schedule_save') {
        foreach (['enabled' => true, 'trigger' => 'interval', 'interval_minutes' => 1440, 'daily_time' => '03:30', 'time_zone' => 'America/Chicago', 'event_action' => '', 'event_status' => 'succeeded', 'use_event_archive' => false, 'arguments' => [], 'window_start' => '', 'window_end' => '', 'window_timezone' => 'America/Chicago', 'label' => $a['id']] as $field => $value) if (!array_key_exists($field, $a)) $a[$field] = $value;
        if (!is_bool($a['enabled']) || !in_array($a['trigger'], ['interval', 'daily', 'event'], true) || !is_int($a['interval_minutes']) || $a['interval_minutes'] < 1 || $a['interval_minutes'] > 525600 || !is_string($a['label']) || strlen($a['label']) > 120 || preg_match('/[\x00-\x1f]/', $a['label'])) ariaFail('invalid request');
        foreach (['time_zone', 'window_timezone'] as $field) if (!is_string($a[$field]) || !in_array($a[$field], DateTimeZone::listIdentifiers(), true) && $a[$field] !== 'UTC') ariaFail('invalid request');
        foreach (['daily_time', 'window_start', 'window_end'] as $field) if (!is_string($a[$field]) || !preg_match($field === 'daily_time' ? '/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/D' : '/^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9])?$/D', $a[$field])) ariaFail('invalid request');
        if (($a['window_start'] === '') !== ($a['window_end'] === '') || $a['window_start'] !== '' && $a['window_start'] === $a['window_end']) ariaFail('invalid request');
        if (!is_string($a['action'] ?? null) || !in_array($a['action'], array_merge(ARIA_AUTOMATION_ACTIONS, ['bridge_auto_update']), true) || !is_array($a['arguments']) || strlen(ariaJson($a['arguments'])) > 16384 || isset($a['arguments']['request_id'])) ariaFail('invalid request');
        if (!is_bool($a['use_event_archive']) || $a['use_event_archive'] && ($a['trigger'] !== 'event' || $a['event_action'] !== 'operations_archive_create' || $a['action'] !== 'operations_archive_verify' || isset($a['arguments']['archive_id']) || isset($a['arguments']['expected_sha256']))) ariaFail('invalid request');
        if ($a['action'] === 'bridge_auto_update') { if ($a['arguments'] !== [] || $a['trigger'] === 'event') ariaFail('invalid request'); }
        else {
            // Validate the actual destination schema now, and again for every run.
            $validationArgs = $a['arguments'];
            if ($a['use_event_archive']) $validationArgs = array_merge($validationArgs, ['archive_id' => 'event-validation', 'expected_sha256' => str_repeat('0', 64)]);
            $args = ariaValidate($a['action'], array_merge($validationArgs, ['request_id' => 'automation-validation']));
            unset($args['request_id']);
            if ($a['use_event_archive']) unset($args['archive_id'], $args['expected_sha256']); $a['arguments'] = $args;
            if (strpos($a['action'], 'script') !== false && isset($args['expected_sha256'], $args['name'])) ariaHash(ariaScriptPath($args['name']), $args['expected_sha256']);
        }
        if (!is_string($a['event_action']) || !in_array($a['event_status'], ['succeeded'], true)) ariaFail('invalid request');
        if ($a['trigger'] === 'event') {
            if ($a['event_action'] === '' || !preg_match('/^[a-z][a-z0-9_]{0,80}$/D', $a['event_action']) || strpos($a['event_action'], 'automation_') === 0 || $a['event_action'] === 'bridge_update') ariaFail('invalid request');
        }
    }
    return $a;
}
function ariaAutomationJobs(string $state, string $status = 'all', int $limit = 50): array {
    $paths = glob("$state/jobs/*.json") ?: []; rsort($paths); $rows = [];
    foreach ($paths as $path) {
        $job = ariaReadJson($path);
        if ($status !== 'all' && ($job['status'] ?? '') !== $status) continue;
        // Results and raw output remain in per-job reads, keeping this bounded.
        $rows[] = array_intersect_key($job, array_flip(['job_id', 'action', 'status', 'created_at', 'started_at', 'finished_at', 'error', 'automation', 'progress']));
        if (count($rows) >= $limit) break;
    }
    return $rows;
}
function ariaAutomationRuntime(string $state): array {
    $runtime = ariaAutomationLoad($state, 'runtime');
    foreach ($runtime as &$item) { $item['seen_event_count'] = count($item['seen_events'] ?? []); unset($item['seen_events']); }
    unset($item); return $runtime;
}
function ariaAutomationRead(string $state, string $action, array $a): array {
    $lock = ariaLock($state, 'automation');
    try {
        ariaAutomationInit($state);
        $config = ariaAutomationLoad($state, 'schedules');
        switch ($action) {
            case 'automation_status': return ['enabled_schedules' => count(array_filter($config['schedules'], fn($s) => $s['enabled'])), 'scheduler' => ariaAutomationLoad($state, 'heartbeat'), 'bridge_check' => ariaAutomationLoad($state, 'bridge-check'), 'runtime' => ariaAutomationRuntime($state), 'notifications' => 'local inbox only; no external messages', 'default_window' => '02:00-06:00 America/Chicago', 'first_default_check' => $config['schedules']['bridge-daily-update']['not_before'] ?? null];
            case 'automation_schedules': return ['schedules' => array_values(array_map('ariaAutomationPublic', $config['schedules'])), 'arguments_redacted' => true];
            case 'automation_jobs': return ['jobs' => ariaAutomationJobs($state, $a['status'] ?? 'all', $a['limit'] ?? 50), 'running_jobs_cancelable' => false];
            case 'automation_inbox': return ['notifications' => array_values(array_filter(ariaAutomationLoad($state, 'inbox'), fn($n) => !empty($a['include_acknowledged']) || !$n['acknowledged'])), 'delivery' => 'local only'];
            case 'automation_dashboard': return ariaAutomationDashboard($state);
        }
        ariaFail('unsupported action');
    } finally { ariaUnlock($lock); }
}
function ariaAutomationExecute(string $state, array $job): array {
    $a = $job['arguments']; $action = $job['action'];
    if ($action === 'automation_bridge_check') return ariaAutomationBridgeCheck($state, false);
    $lock = ariaLock($state, 'automation');
    try {
        ariaAutomationInit($state);
        $config = ariaAutomationLoad($state, 'schedules');
        if ($action === 'automation_schedule_save' || $action === 'automation_schedule_delete') {
            $old = $config['schedules'][$a['id']] ?? null;
            if ($old === null && $a['expected_sha256'] !== '' || $old !== null && !hash_equals(ariaAutomationHash($old), $a['expected_sha256'])) ariaFail('hash mismatch');
            if ($action === 'automation_schedule_delete') {
                if ($old === null) ariaFail('not found');
                unset($config['schedules'][$a['id']]);
                ariaAutomationSave($state, 'schedules', $config);
                return ['id' => $a['id'], 'deleted' => true];
            }
            if ($old === null && count($config['schedules']) >= 128) ariaFail('queue full');
            $schedule = $a; unset($schedule['request_id'], $schedule['expected_sha256']);
            $schedule['created_at'] = $old['created_at'] ?? ariaNow(); $schedule['updated_at'] = ariaNow();
            $schedule['generation'] = bin2hex(random_bytes(8)); $schedule['not_before'] = ariaAutomationClock() + 60;
            $config['schedules'][$a['id']] = $schedule;
            ariaAutomationSave($state, 'schedules', $config);
            return ariaAutomationPublic($schedule);
        }
        if ($action === 'automation_inbox_ack') {
            $noticeLock = ariaLock($state, 'automation-inbox');
            try {
            $inbox = ariaAutomationLoad($state, 'inbox'); $found = false;
            foreach ($inbox as &$notice) if ($notice['id'] === $a['id']) { $notice['acknowledged'] = true; $found = true; }
            unset($notice);
            if (!$found) ariaFail('not found');
            ariaAutomationSave($state, 'inbox', $inbox);
            return ['id' => $a['id'], 'acknowledged' => true];
            } finally { ariaUnlock($noticeLock); }
        }
        if ($action === 'automation_bridge_check') return ariaAutomationBridgeCheck($state, false);
        ariaFail('unsupported action');
    } finally { ariaUnlock($lock); }
}
/** Immediate queue control, serialized with enqueue and worker claim. */
function ariaAutomationControl(string $state, string $action, array $a): array {
    $a = ariaAutomationValidate($action, $a);
    if ($action !== 'automation_job_cancel') ariaFail('unsupported action');
    $lock = ariaLock($state, 'queue');
    try {
        $request = $a['request_id']; unset($a['request_id']);
        $fingerprint = hash('sha256', ariaJson([$action, ariaCanonical($a)]));
        $receiptPath = "$state/requests/$request.json";
        if (is_file($receiptPath)) {
            $receipt = ariaReadJson($receiptPath);
            if (!hash_equals($receipt['fingerprint'], $fingerprint)) ariaFail('request_id conflict');
            $result = "$state/jobs/{$receipt['job_id']}.json";
            return is_file($result) ? array_merge(ariaReadJson($result)['result'], ['deduplicated' => true]) : ['status' => 'unknown', 'deduplicated' => true];
        }
        if (count(glob("$state/requests/*.json") ?: []) >= ARIA_MAX_RECEIPTS) ariaFail('deduplication capacity reached');
        $path = "$state/jobs/{$a['job_id']}.json";
        if (!is_file($path)) ariaFail('job unknown');
        $target = ariaReadJson($path);
        if ($target['status'] !== 'queued') ariaFail('management busy');
        $id = gmdate('YmdHis').'-'.bin2hex(random_bytes(12));
        ariaAtomic($receiptPath, ariaJson(['fingerprint' => $fingerprint, 'job_id' => $id]));
        $target['status'] = 'cancelled'; $target['finished_at'] = ariaNow(); $target['error'] = 'cancelled before execution'; unset($target['arguments']);
        ariaAtomic($path, ariaJson($target));
        $result = ['job_id' => $a['job_id'], 'status' => 'cancelled', 'deduplicated' => false];
        ariaAtomic("$state/jobs/$id.json", ariaJson(['job_id' => $id, 'action' => $action, 'status' => 'succeeded', 'created_at' => ariaNow(), 'started_at' => ariaNow(), 'finished_at' => ariaNow(), 'result' => $result, 'error' => null]));
        ariaPrune($state);
        return $result;
    } finally { ariaUnlock($lock); }
}
function ariaAutomationWindow(array $schedule, int $now): bool {
    if ($schedule['window_start'] === '') return true;
    $local = (new DateTimeImmutable('@'.$now))->setTimezone(new DateTimeZone($schedule['window_timezone']))->format('H:i');
    return $schedule['window_start'] < $schedule['window_end'] ? $local >= $schedule['window_start'] && $local < $schedule['window_end'] : $local >= $schedule['window_start'] || $local < $schedule['window_end'];
}
function ariaAutomationSlot(array $schedule, int $now): ?string {
    if ($now < $schedule['not_before']) return null;
    if ($schedule['trigger'] === 'interval') return (string)intdiv($now - $schedule['not_before'], $schedule['interval_minutes'] * 60);
    if ($schedule['trigger'] === 'daily') {
        $local = (new DateTimeImmutable('@'.$now))->setTimezone(new DateTimeZone($schedule['time_zone']));
        if ($local->format('H:i') < $schedule['daily_time']) return null;
        return $local->format('Y-m-d');
    }
    return null;
}
function ariaAutomationIdle(string $state): bool {
    if (ariaBridgeBusy($state)) return false;
    foreach (glob("$state/jobs/*.json") ?: [] as $path) if (in_array(ariaReadJson($path)['status'], ['queued', 'running', 'unknown'], true)) return false;
    return true;
}
function ariaAutomationGit(string $source, array $args): array {
    return ariaRun(array_merge([ariaPath('ARIA_AUTOMATION_GIT', '/usr/bin/git'), '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false', '-C', $source], $args), 120);
}
function ariaAutomationGuard(string $state, string $id, string $revision, callable $effect): ?array {
    // Schedule edits and queue submissions share this lock. Network checks run
    // before taking it, so an edit can invalidate work already in flight.
    $lock = ariaLock($state, 'automation');
    try {
        $config = ariaAutomationLoad($state, 'schedules');
        $current = $config['schedules'][$id] ?? null;
        if ($current === null || !$current['enabled'] || !hash_equals($revision, ariaAutomationHash($current)) || !ariaAutomationWindow($current, ariaAutomationClock()) || ariaAutomationClock() < $current['not_before']) return null;
        return $effect();
    } finally { ariaUnlock($lock); }
}
function ariaAutomationBridgeCheck(string $state, bool $install, ?array $scheduleGuard = null): array {
    $result = ['checked_at' => ariaNow(), 'status' => 'check_failed', 'auto_install' => $install, 'installed_revision' => null, 'available_revision' => null, 'ci_verified' => false];
    try {
        $source = realpath(ariaPath('ARIA_AUTOMATION_SOURCE', dirname(__DIR__, 2)));
        if ($source === false) ariaFail('command failed');
        $text = function(array $args) use ($source): string { $r = ariaAutomationGit($source, $args); if ($r['exit_code'] !== 0 || $r['truncated']) ariaFail('command failed'); return trim($r['output']); };
        if ($text(['rev-parse', '--show-toplevel']) !== $source || $text(['symbolic-ref', '--quiet', '--short', 'HEAD']) !== 'main' || $text(['status', '--porcelain=v1', '--untracked-files=all']) !== '') ariaFail('command failed');
        $origin = $text(['remote', 'get-url', '--all', 'origin']);
        if (!preg_match('~^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)kevinwaynekelly/(kevin-cogs|aria-gpt-bridge)(?:\.git)?$~D', $origin, $matches)) ariaFail('command failed');
        $repository = 'kevinwaynekelly/'.$matches[1];
        $head = $text(['rev-parse', '--verify', 'HEAD^{commit}']);
        $result['checkout_revision'] = $head;
        $installed = is_file("$state/installed-revision.json") && !is_link("$state/installed-revision.json") ? ariaReadJson("$state/installed-revision.json") : [];
        $result['installed_revision_source'] = 'checkout_fallback';
        if (is_string($installed['revision'] ?? null) && preg_match('/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/D', $installed['revision'])) { $head = $installed['revision']; $result['installed_revision_source'] = 'installed_metadata'; }
        $text(['fetch', '--no-tags', '--no-recurse-submodules', '--force', 'origin', 'refs/heads/main:refs/aria-automation/main']);
        $target = $text(['rev-parse', '--verify', 'refs/aria-automation/main^{commit}']);
        foreach ([$head, $target] as $sha) if (!preg_match('/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/D', $sha)) ariaFail('command failed');
        $result['installed_revision'] = $head; $result['available_revision'] = $target; $result['repository'] = $repository;
        if ($head === $target) { $result['status'] = 'current'; }
        else {
            $text(['merge-base', '--is-ancestor', $head, $target]);
            $changed = $text(['diff', '--name-only', "$head..$target", '--', 'tools/aria_bridge']);
            $result['bridge_changed'] = $changed !== '';
            if ($changed === '') $result['status'] = 'no_bridge_changes';
            else {
                // No token is exposed to the bridge. Private-repository API failures defer installation.
                $response = ariaRun([ariaPath('ARIA_AUTOMATION_CURL', '/usr/bin/curl'), '--fail', '--silent', '--show-error', '--max-time', '30', '--max-filesize', '524288', '--proto', '=https', '--header', 'Accept: application/vnd.github+json', '--header', 'User-Agent: aria-bridge-updater', "https://api.github.com/repos/$repository/commits/$target/check-runs?per_page=100"], 40);
                if ($response['exit_code'] !== 0 || $response['truncated']) ariaFail('command failed');
                $checks = json_decode($response['output'], true);
                if (!is_array($checks) || !isset($checks['check_runs']) || !is_array($checks['check_runs']) || ($checks['total_count'] ?? 101) > 100) ariaFail('command failed');
                $passed = []; $latestChecks = [];
                foreach ($checks['check_runs'] as $check) if (is_string($check['name'] ?? null) && ($check['app']['slug'] ?? '') === 'github-actions' && (!isset($latestChecks[$check['name']]) || ($check['id'] ?? 0) > ($latestChecks[$check['name']]['id'] ?? 0))) $latestChecks[$check['name']] = $check;
                foreach ($latestChecks as $check) if (($check['head_sha'] ?? '') === $target && ($check['app']['slug'] ?? '') === 'github-actions' && ($check['status'] ?? '') === 'completed' && ($check['conclusion'] ?? '') === 'success' && preg_match('~^https://github\.com/'.preg_quote($repository, '~').'/actions/runs/[0-9]+/~', $check['details_url'] ?? '')) $passed[$check['name']] = true;
                $result['ci_verified'] = isset($passed['checks (3.10)'], $passed['checks (3.11)']);
                $result['status'] = $result['ci_verified'] ? 'available' : 'ci_pending';
                if ($install && $result['ci_verified']) {
                    if (!ariaAutomationIdle($state)) $result['status'] = 'deferred_busy';
                    else {
                        $submit = fn() => ariaBridgeQueue($state, ['request_id' => 'auto-bridge-'.substr(hash('sha256', $target), 0, 40)], $target);
                        $queued = $scheduleGuard === null ? $submit() : ariaAutomationGuard($state, $scheduleGuard['id'], $scheduleGuard['revision'], $submit);
                        if ($queued === null) $result['status'] = 'deferred_schedule';
                        else { $result['update'] = $queued; $result['status'] = in_array($queued['status'], ['queued', 'running', 'succeeded'], true) ? 'update_queued' : 'previous_update_failed'; }
                    }
                }
            }
        }
    } catch (Throwable $e) { $result['status'] = $e->getMessage() === 'management busy' ? 'deferred_busy' : 'check_failed'; $result['error'] = 'Repository, checkout, network or CI verification failed; no update started.'; }
    ariaAutomationSave($state, 'bridge-check', $result);
    return $result;
}
function ariaAutomationDashboard(string $state): array {
    $jobs = ariaAutomationJobs($state, 'all', 30);
    $escape = fn($v) => htmlspecialchars((string)$v, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    $html = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Aria management</title><style>body{font:16px system-ui;background:#121b28;color:#edf3fa;margin:2rem;max-width:1000px}table{border-collapse:collapse;width:100%}th,td{padding:.6rem;text-align:left;border-bottom:1px solid #35445a}small{color:#a8bad0}</style><h1>Aria management</h1><p>Local snapshot, '.$escape(ariaNow()).'</p><p><small>This page has no listener, credentials or mutation controls. Refresh it through the dashboard tool or open the generated file on the host.</small></p><h2>Recent jobs</h2><table><tr><th>Job</th><th>Action</th><th>Status</th></tr>';
    foreach ($jobs as $job) $html .= '<tr><td>'.$escape($job['job_id']).'</td><td>'.$escape($job['action']).'</td><td>'.$escape($job['status']).'</td></tr>';
    $html .= '</table><h2>Bridge updater</h2><p>'.$escape(ariaAutomationLoad($state, 'bridge-check')['status'] ?? 'Not checked yet').'</p><h2>Local notifications</h2><ul>';
    foreach (array_slice(ariaAutomationLoad($state, 'inbox'), -20) as $notice) if (!$notice['acknowledged']) $html .= '<li>'.$escape($notice['message']).'</li>';
    $html .= '</ul></html>';
    $path = ariaAutomationDirectory($state).'/dashboard.html'; ariaAtomic($path, $html);
    return ['html' => $html, 'path' => $path, 'generated_at' => ariaNow(), 'listener' => false, 'read_only' => true];
}
function ariaAutomationTick(string $state): array {
    $lock = ariaLock($state, 'automation-tick', true);
    try {
        if (function_exists('ariaExtensionSpecs')) ariaExtensionSpecs();
        ariaAutomationInit($state); $now = ariaAutomationClock();
        $config = ariaAutomationLoad($state, 'schedules'); $runtime = ariaAutomationLoad($state, 'runtime');
        if (function_exists('ariaDiagnosticsRead') && ariaAutomationIdle($state)) {
            $sample = ariaAutomationLoad($state, 'resource-sample');
            if (($sample['timestamp'] ?? 0) <= $now - 300) {
                try {
                    $resources = ariaDiagnosticsRead($state, 'diagnostics_resources', []);
                    $resourcesOk = ($resources['containers']['exit_code'] ?? 1) === 0 && empty($resources['containers']['truncated']);
                    $sample = ['timestamp' => $now, 'status' => $resourcesOk ? 'succeeded' : 'failed', 'resource_status' => $resourcesOk ? 'succeeded' : 'failed'];
                    if (function_exists('ariaDiagnosticsMonitor') && ariaAutomationIdle($state)) {
                        $sample['monitor'] = ariaDiagnosticsMonitor($state);
                        $monitorStatus = $sample['monitor']['status'] ?? 'failed';
                        if ($monitorStatus !== 'succeeded' || !$resourcesOk) $sample['status'] = !$resourcesOk && $monitorStatus === 'failed' ? 'failed' : 'partial';
                    }
                }
                catch (Throwable $e) { $sample = ['timestamp' => $now, 'status' => 'failed', 'error' => ariaError($e)]; }
                ariaAutomationSave($state, 'resource-sample', $sample);
            }
        }
        $jobs = [];
        foreach (glob("$state/jobs/*.json") ?: [] as $path) {
            $job = ariaReadJson($path); $jobs[] = $job;
            if (in_array($job['status'], ['failed', 'partial', 'unknown'], true)) ariaAutomationNotice($state, 'job-'.substr(hash('sha256', $job['job_id'].$job['status']), 0, 40), 'job_'.$job['status'], $job['action'].' '.$job['status'], ['job_id' => $job['job_id']]);
        }
        $bridgeJob = ariaBridgeLatest($state);
        if ($bridgeJob !== null && in_array($bridgeJob['status'], ['failed', 'unknown'], true)) ariaAutomationNotice($state, 'bridge-'.substr(hash('sha256', $bridgeJob['job_id'].$bridgeJob['status']), 0, 40), 'bridge_update_'.$bridgeJob['status'], 'Bridge update '.$bridgeJob['status'].'; review the dedicated update status.', ['job_id' => $bridgeJob['job_id']]);
        foreach ($config['schedules'] as $id => $schedule) {
            if (!$schedule['enabled'] || !ariaAutomationWindow($schedule, $now) || $now < $schedule['not_before']) continue;
            $revision = ariaAutomationHash($schedule);
            $stateFor = $runtime[$id] ?? [];
            if (($stateFor['revision'] ?? '') !== $revision) $stateFor = ['revision' => $revision, 'seen_events' => []];
            if (isset($stateFor['blocked_error']) || ($stateFor['retry_after'] ?? 0) > $now) continue;
            $sourceJob = null; $slot = ariaAutomationSlot($schedule, $now);
            if ($schedule['trigger'] === 'event') {
                foreach ($jobs as $candidate) {
                    $chain = $candidate['automation']['chain'] ?? [];
                    if ($candidate['action'] !== $schedule['event_action'] || $candidate['status'] !== $schedule['event_status'] || (strtotime($candidate['finished_at'] ?? '') ?: 0) < (strtotime($schedule['updated_at']) ?: 0) || isset($stateFor['seen_events'][$candidate['job_id']]) || count($chain) >= 4 || in_array($id, $chain, true)) continue;
                    $sourceJob = $candidate; $slot = $candidate['job_id']; break;
                }
            }
            if ($slot === null || ($stateFor['last_slot'] ?? null) === $slot) continue;
            if (!ariaAutomationIdle($state)) { $stateFor['deferred_reason'] = 'management busy'; $runtime[$id] = $stateFor; continue; }
            try {
                if ($schedule['action'] === 'bridge_auto_update') {
                    $result = ariaAutomationBridgeCheck($state, true, ['id' => $id, 'revision' => $revision]);
                    if ($result['status'] === 'deferred_schedule') continue;
                    if (in_array($result['status'], ['ci_pending', 'deferred_busy', 'check_failed'], true)) { $stateFor['retry_after'] = $now + 900; $stateFor['deferred_reason'] = $result['status']; $runtime[$id] = $stateFor; continue; }
                } else {
                    $submissionArgs = $schedule['arguments'];
                    if (!empty($schedule['use_event_archive'])) {
                        if (!is_string($sourceJob['result']['archive_id'] ?? null) || !is_string($sourceJob['result']['sha256'] ?? null)) ariaFail('invalid request');
                        $submissionArgs['archive_id'] = $sourceJob['result']['archive_id'];
                        $submissionArgs['expected_sha256'] = $sourceJob['result']['sha256'];
                    }
                    $args = ariaValidate($schedule['action'], array_merge($submissionArgs, ['request_id' => 'auto-'.substr(hash('sha256', $id.$revision.$slot), 0, 48)]));
                    if (strpos($schedule['action'], 'script') !== false && isset($args['expected_sha256'], $args['name'])) ariaHash(ariaScriptPath($args['name']), $args['expected_sha256']);
                    $GLOBALS['ARIA_AUTOMATION_CONTEXT'] = ['schedule_id' => $id, 'chain' => array_merge($sourceJob['automation']['chain'] ?? [], [$id]), 'source_job_id' => $sourceJob['job_id'] ?? null];
                    try { $result = ariaAutomationGuard($state, $id, $revision, fn() => ariaDispatch($state, ['action' => $schedule['action'], 'arguments' => $args])); }
                    finally { unset($GLOBALS['ARIA_AUTOMATION_CONTEXT']); }
                    if ($result === null) continue;
                    if (in_array($result['status'] ?? '', ['unknown', 'expired'], true)) ariaFail('management busy');
                }
                $stateFor['last_slot'] = $slot; $stateFor['last_run_at'] = ariaNow(); $stateFor['last_result'] = $result;
                unset($stateFor['deferred_reason'], $stateFor['retry_after']);
                if ($sourceJob !== null) { $stateFor['seen_events'][$sourceJob['job_id']] = true; $stateFor['seen_events'] = array_slice($stateFor['seen_events'], -512, null, true); }
            } catch (Throwable $e) {
                if (in_array($e->getMessage(), ['management busy', 'policy denied'], true)) { $stateFor['deferred_reason'] = $e->getMessage(); $stateFor['retry_after'] = $now + ($e->getMessage() === 'policy denied' ? 300 : 60); }
                else {
                    $stateFor['blocked_error'] = ariaError($e);
                    ariaAutomationNotice($state, 'schedule-'.substr(hash('sha256', $id.$revision), 0, 40), 'schedule_blocked', 'Schedule '.$id.' is blocked; review and save it again.', ['schedule_id' => $id, 'error' => ariaError($e)]);
                }
            }
            $runtime[$id] = $stateFor;
            // Persist each acknowledgement before considering another effect.
            ariaAutomationSave($state, 'runtime', $runtime);
        }
        $runtime = array_intersect_key($runtime, ariaAutomationLoad($state, 'schedules')['schedules']);
        ariaAutomationSave($state, 'runtime', $runtime);
        $heartbeat = ['checked_at' => ariaNow(), 'timestamp' => $now, 'pid' => getmypid(), 'status' => 'running'];
        ariaAutomationSave($state, 'heartbeat', $heartbeat);
        ariaAutomationDashboard($state);
        return $heartbeat;
    } finally { ariaUnlock($lock); }
}
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    require_once __DIR__.'/host-agent.php';
    try {
        if (count($argv) !== 3 || !in_array($argv[1], ['tick', 'daemon'], true) || $argv[2] === '' || $argv[2][0] !== '/') ariaFail('invalid request');
        ariaInit($argv[2]);
        $GLOBALS['ARIA_WORKER_STATE'] = $argv[2];
        $GLOBALS['ARIA_PROCESS_RECORD'] = 'active-automation-process.json';
        do {
            try { ariaAutomationTick($argv[2]); }
            catch (Throwable $e) { if ($argv[1] === 'tick') throw $e; fwrite(STDERR, ariaError($e)."\n"); }
            if ($argv[1] !== 'daemon') break;
            sleep(60);
        } while (true);
    } catch (Throwable $e) { fwrite(STDERR, ariaError($e)."\n"); exit(1); }
}
