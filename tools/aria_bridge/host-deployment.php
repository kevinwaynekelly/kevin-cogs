<?php
/** Reviewed deployments, policies and copy-based appdata operations. Included by host-agent.php. */
declare(strict_types=1);

function ariaDeploymentStore(string $state, string $kind): string {
    $path = "$state/$kind";
    if (is_link($path)) ariaFail('internal error');
    if (!is_dir($path) && !mkdir($path, 0700)) ariaFail('internal error');
    return $path;
}
function ariaDeploymentDefaultPolicy(): array {
    return ['allowed_actions' => ['inspect', 'logs', 'start', 'stop', 'restart', 'update', 'deploy', 'save', 'restore', 'pin', 'test', 'migrate', 'exec'], 'hold_updates' => false, 'require_healthcheck' => false, 'health_timeout_seconds' => 60, 'maintenance_utc_hours' => [], 'require_stopped_containers' => [], 'require_idle_profiles' => []];
}
function ariaDeploymentPolicy(string $state, string $name): array {
    ariaName($name, 'container');
    $path = "$state/container-policies/$name.json";
    if (is_link($path) || is_link(dirname($path))) ariaFail('internal error');
    if (!is_file($path)) return ['name' => $name, 'sha256' => '', 'policy' => ariaDeploymentDefaultPolicy(), 'configured' => false];
    $text = ariaReadFile($path, ARIA_MAX_XML); $policy = json_decode($text, true);
    if (!is_array($policy)) ariaFail('internal error');
    return ['name' => $name, 'sha256' => hash('sha256', $text), 'policy' => array_replace(ariaDeploymentDefaultPolicy(), $policy), 'configured' => true];
}
function ariaDeploymentPolicyCheck(array $p): array {
    $defaults = ariaDeploymentDefaultPolicy();
    if (array_diff(array_keys($p), array_keys($defaults))) ariaFail('invalid request');
    foreach (['hold_updates', 'require_healthcheck'] as $k) if (isset($p[$k]) && !is_bool($p[$k])) ariaFail('invalid request');
    if (isset($p['health_timeout_seconds']) && (!is_int($p['health_timeout_seconds']) || $p['health_timeout_seconds'] < 1 || $p['health_timeout_seconds'] > 600)) ariaFail('invalid request');
    foreach (['allowed_actions', 'maintenance_utc_hours', 'require_stopped_containers', 'require_idle_profiles'] as $k) {
        if (!isset($p[$k])) continue;
        if (!is_array($p[$k]) || count($p[$k]) > ($k === 'require_idle_profiles' ? 8 : 32)) ariaFail('invalid request');
        foreach ($p[$k] as $v) {
            if ($k === 'allowed_actions' && !in_array($v, $defaults['allowed_actions'], true)) ariaFail('invalid request');
            if ($k === 'maintenance_utc_hours' && (!is_int($v) || $v < 0 || $v > 23)) ariaFail('invalid request');
            if ($k === 'require_stopped_containers') ariaName($v, 'container');
            if ($k === 'require_idle_profiles' && (!is_string($v) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/D', $v))) ariaFail('invalid request');
        }
    }
    return array_replace($defaults, $p);
}
/** Called at admission and execution; external root execution can bypass bridge policy. */
function ariaDeploymentAuthorize(string $state, string $action, array $a): void {
    $mapping = ['operations_container_exec' => 'exec', 'container_inspect' => 'inspect', 'container_logs' => 'logs', 'template_get' => 'inspect', 'template_save' => 'save', 'template_deploy' => 'deploy', 'container_update' => 'update', 'deployment_template_preview' => 'inspect', 'deployment_template_history' => 'inspect', 'deployment_template_restore' => 'restore', 'deployment_config_drift' => 'inspect', 'deployment_image_details' => 'inspect', 'deployment_image_pin' => 'pin', 'deployment_upgrade_test' => 'test', 'deployment_appdata_migrate' => 'migrate'];
    $operation = $action === 'container_action' ? ($a['action'] ?? '') : ($mapping[$action] ?? null);
    if ($operation === null) return;
    $names = [];
    if (isset($a['template'])) {
        $path = ariaTemplatePath($a['template'], false);
        if (is_file($path)) $names[] = (string)ariaXML(ariaReadFile($path, ARIA_MAX_XML))->Name;
        if (in_array($action, ['template_save', 'deployment_template_restore'], true) && isset($a['xml'])) $names[] = (string)ariaXML($a['xml'])->Name;
    } elseif (isset($a['name'])) $names[] = ariaName($a['name'], 'container');
    foreach (array_unique($names) as $name) {
        $policy = ariaDeploymentPolicy($state, $name)['policy'];
        if (!in_array($operation, $policy['allowed_actions'], true)) ariaFail('policy denied');
        if (in_array($operation, ['inspect', 'logs'], true)) continue;
        if (ariaProtected($name)) ariaFail('protected container');
        if ($policy['hold_updates'] && in_array($operation, ['update', 'deploy', 'pin'], true)) ariaFail('policy denied');
        if ($policy['maintenance_utc_hours'] && !in_array((int)gmdate('G'), $policy['maintenance_utc_hours'], true)) ariaFail('policy denied');
        if ($policy['require_idle_profiles']) {
            if (!function_exists('ariaApplicationsActivity')) ariaFail('policy denied');
            $activity = ariaApplicationsActivity($state, $policy['require_idle_profiles']);
            if (empty($activity['idle']) || !empty($activity['unknown'])) ariaFail('policy denied');
        }
        foreach ($policy['require_stopped_containers'] as $other) {
            $info = ariaInspect($other, false);
            if ($info !== null && !empty($info['State']['Running'])) ariaFail('policy denied');
        }
    }
}
/** Docker health must pass before removing the old deployment retained for rollback. */
function ariaDeploymentHealth(string $state, string $name): array {
    $policy = ariaDeploymentPolicy($state, $name)['policy'];
    $deadline = microtime(true) + $policy['health_timeout_seconds'];
    $id = null;
    do {
        $info = ariaInspect($name);
        if ($id !== null && $id !== $info['Id']) ariaFail('hash mismatch');
        $id = $info['Id'];
        if (empty($info['State']['Running']) || !empty($info['State']['Dead']) || !empty($info['State']['OOMKilled'])) ariaFail('command failed');
        $health = $info['State']['Health']['Status'] ?? null;
        if ($health === 'healthy') return ['application_health_verified' => true, 'health_status' => 'healthy', 'checked_at' => ariaNow()];
        if ($health === null) {
            if ($policy['require_healthcheck']) ariaFail('healthcheck required');
            return ['application_health_verified' => false, 'health_status' => 'not configured', 'checked_at' => ariaNow()];
        }
        if ($health === 'unhealthy') ariaFail('command failed');
        if (microtime(true) >= $deadline) ariaFail('operation timed out');
        usleep(250000);
    } while (true);
}
function ariaDeploymentSaveJson(string $state, string $kind, string $name, array $data, string $expected): array {
    ariaName($name, 'container'); $dir = ariaDeploymentStore($state, $kind); $path = "$dir/$name.json";
    if (is_link($path)) ariaFail('invalid request');
    if (is_file($path)) ariaHash($path, $expected); elseif ($expected !== '') ariaFail('hash mismatch');
    $text = ariaJson($data); ariaAtomic($path, $text);
    return ['name' => $name, 'sha256' => hash('sha256', $text)];
}
function ariaDeploymentWriteTemplate(string $state, string $jobId, string $template, string $xml, string $expected): array {
    ariaName($jobId, 'id'); $path = ariaTemplatePath($template); ariaHash($path, $expected);
    $old = ariaReadFile($path, ARIA_MAX_XML); $tree = ariaXML($xml);
    if (ariaProtected((string)$tree->Name) || ariaProtected((string)ariaXML($old)->Name)) ariaFail('protected container');
    ariaAtomic("$state/backups/$jobId.xml", $old);
    ariaAtomic("$state/backups/$jobId.meta.json", ariaJson(['template' => $template, 'name' => (string)$tree->Name, 'created_at' => ariaNow()]));
    ariaHash($path, $expected); ariaAtomic($path, $xml);
    return ['template' => $template, 'sha256' => hash('sha256', $xml), 'backup_id' => $jobId, 'deployed' => false];
}
function ariaDeploymentTemplateFields(string $xml): array {
    $tree = ariaXML(ariaRedactedXML($xml)[0]); $fields = [];
    foreach ($tree->children() as $node) {
        $tag = $node->getName();
        if ($tag === 'Config') {
            $key = 'Config/'.(string)$node['Type'].'/'.(string)$node['Target'];
            $fields[$key] = ['value' => (string)$node, 'mode' => (string)$node['Mode'], 'default' => (string)$node['Default']];
        } elseif (count($node->children()) === 0) $fields[$tag] = (string)$node;
        else $fields[$tag] = (string)$node->asXML();
    }
    ksort($fields); return $fields;
}
function ariaDeploymentHistory(string $state, string $template): array {
    $current = ariaXML(ariaReadFile(ariaTemplatePath($template), ARIA_MAX_XML)); $rows = [];
    foreach (glob("$state/backups/*.xml") ?: [] as $path) {
        if (is_link($path)) continue;
        try {
            $id = basename($path, '.xml'); ariaName($id, 'id');
            $xml = ariaReadFile($path, ARIA_MAX_XML); $tree = ariaXML($xml);
            $meta = is_file("$state/backups/$id.meta.json") ? ariaReadJson("$state/backups/$id.meta.json") : [];
            if (isset($meta['template']) ? $meta['template'] !== $template : (string)$tree->Name !== (string)$current->Name) continue;
            $rows[] = ['backup_id' => $id, 'sha256' => hash('sha256', $xml), 'created_at' => $meta['created_at'] ?? gmdate('Y-m-d\TH:i:s\Z', filemtime($path)), 'image' => (string)$tree->Repository];
        } catch (Throwable $e) { continue; }
    }
    usort($rows, function ($a, $b) { return strcmp($b['created_at'], $a['created_at']); });
    return ['template' => $template, 'backups' => array_slice($rows, 0, 100), 'truncated' => count($rows) > 100];
}
function ariaDeploymentMounts(SimpleXMLElement $tree): array {
    $mounts = [];
    foreach ($tree->Config as $config) if (strtolower((string)$config['Type']) === 'path') {
        $mounts[] = ['source' => (string)$config !== '' ? (string)$config : (string)$config['Default'], 'target' => (string)$config['Target'], 'mode' => (string)$config['Mode'] ?: 'rw'];
    }
    foreach ($tree->Data->Volume ?? [] as $volume) $mounts[] = ['source' => (string)$volume->HostDir, 'target' => (string)$volume->ContainerDir, 'mode' => (string)$volume->Mode ?: 'rw'];
    return $mounts;
}
function ariaDeploymentImage(string $name): array {
    $container = ariaInspect($name); $r = ariaDocker(['image', 'inspect', $container['Image']]);
    if ($r['truncated']) ariaFail('command failed');
    $image = json_decode($r['output'], true)[0] ?? null;
    if (!is_array($image) || ($image['Id'] ?? '') !== $container['Image']) ariaFail('command failed');
    $labels = $image['Config']['Labels'] ?? [];
    foreach ($labels as $key => $value) $labels[$key] = ariaScrub((string)$value, [], 2048);
    return ['name' => $name, 'image_id' => $image['Id'], 'configured_image' => $container['Config']['Image'] ?? null, 'digests' => $image['RepoDigests'] ?? [], 'created' => $image['Created'] ?? null, 'architecture' => $image['Architecture'] ?? null, 'os' => $image['Os'] ?? null, 'metadata' => array_intersect_key($labels, array_flip(['org.opencontainers.image.version', 'org.opencontainers.image.revision', 'org.opencontainers.image.source', 'org.opencontainers.image.url', 'org.opencontainers.image.documentation'])), 'pulled' => false];
}
function ariaDeploymentDrift(string $name): array {
    $row = ariaFindTemplate($name); $tree = ariaXML(ariaReadFile(ariaTemplatePath($row['template']), ARIA_MAX_XML)); $info = ariaInspect($name); $changes = [];
    $checks = ['image' => [(string)$tree->Repository, $info['Config']['Image'] ?? null], 'privileged' => [strtolower((string)$tree->Privileged) === 'true', !empty($info['HostConfig']['Privileged'])]];
    if ((string)$tree->ExtraParams === '') $checks['network'] = [ariaRequestedNetwork($tree), $info['HostConfig']['NetworkMode'] ?? null];
    foreach ($checks as $key => [$expected, $actual]) if ($expected !== $actual) $changes[] = ['field' => $key, 'expected' => $expected, 'actual' => $actual];
    $expectedMounts = [];
    foreach (ariaDeploymentMounts($tree) as $mount) {
        $expectedMounts[$mount['target']] = true; $found = false;
        foreach ($info['Mounts'] ?? [] as $actual) if (($actual['Destination'] ?? '') === $mount['target']) {
            $found = true; $source = ($actual['Type'] ?? '') === 'volume' ? ($actual['Name'] ?? '') : ($actual['Source'] ?? '');
            if ($source !== $mount['source'] || !empty($actual['RW']) !== (strpos($mount['mode'], 'ro') !== 0)) $changes[] = ['field' => 'mount:'.$mount['target'], 'expected' => $mount, 'actual' => ['source' => $source, 'writable' => !empty($actual['RW'])]];
        }
        if (!$found) $changes[] = ['field' => 'mount:'.$mount['target'], 'expected' => $mount, 'actual' => null];
    }
    foreach ($info['Mounts'] ?? [] as $mount) if (!isset($expectedMounts[$mount['Destination'] ?? ''])) $changes[] = ['field' => 'undeclared_mount', 'actual' => ['source' => $mount['Source'] ?? '', 'target' => $mount['Destination'] ?? '']];
    foreach ($tree->Config as $config) if (strtolower((string)$config['Type']) === 'port') {
        $target = (string)$config['Target'].'/'.((string)$config['Mode'] ?: 'tcp'); $port = (string)$config !== '' ? (string)$config : (string)$config['Default'];
        $actual = array_column($info['NetworkSettings']['Ports'][$target] ?? [], 'HostPort');
        if (ariaRequestedNetwork($tree) !== 'host' && !in_array($port, $actual, true)) $changes[] = ['field' => 'port:'.$target, 'expected' => $port, 'actual' => $actual];
    }
    return ['name' => $name, 'template' => $row['template'], 'template_sha256' => $row['sha256'], 'differences' => $changes, 'checked_at' => ariaNow(), 'scope' => 'image reference, network, privilege, declared mounts and ports; opaque ExtraParams and secrets are not compared'];
}
function ariaDeploymentStack(string $state, string $name): array {
    ariaName($name, 'container'); $path = ariaFile("$state/stacks", "$name.json"); $text = ariaReadFile($path, ARIA_MAX_XML);
    $definition = json_decode($text, true); if (!is_array($definition)) ariaFail('internal error');
    return ['name' => $name, 'sha256' => hash('sha256', $text), 'definition' => $definition];
}
function ariaDeploymentStackOrder(array $definition, bool $checkHashes = true): array {
    if (array_keys($definition) !== ['members'] || !is_array($definition['members']) || count($definition['members']) < 1 || count($definition['members']) > 32) ariaFail('invalid request');
    $members = []; $names = [];
    foreach ($definition['members'] as $member) {
        if (!is_array($member) || array_diff(array_keys($member), ['template', 'expected_sha256', 'depends_on'])) ariaFail('invalid request');
        $template = ariaName($member['template'] ?? null, 'template');
        if (isset($members[$template]) || !preg_match('/^[a-f0-9]{64}$/D', $member['expected_sha256'] ?? '') || !is_array($member['depends_on'] ?? null)) ariaFail('invalid request');
        $path = ariaTemplatePath($template); if ($checkHashes) ariaHash($path, $member['expected_sha256']);
        $name = (string)ariaXML(ariaReadFile($path, ARIA_MAX_XML))->Name;
        if (isset($names[$name]) || ariaProtected($name)) ariaFail('invalid request');
        $names[$name] = true; $members[$template] = $member + ['name' => $name];
    }
    foreach ($members as $template => $member) foreach ($member['depends_on'] as $dependency) if (!is_string($dependency) || !isset($members[$dependency]) || $dependency === $template) ariaFail('invalid request');
    $ordered = []; $done = [];
    while (count($done) < count($members)) {
        $progress = false;
        foreach ($members as $template => $member) {
            if (isset($done[$template]) || array_diff($member['depends_on'], array_keys($done))) continue;
            $ordered[] = $member; $done[$template] = true; $progress = true;
        }
        if (!$progress) ariaFail('invalid request');
    }
    return $ordered;
}
/** Resolve existing directory or new leaf, rejecting symlinks in every component. */
function ariaDeploymentAppdataPath(string $path, bool $existing = true): string {
    $root = rtrim(ariaPath('ARIA_AGENT_APPDATA', '/mnt/user/appdata'), '/');
    if ($path === '' || $path[0] !== '/' || strlen($path) > 4096 || preg_match('/[\x00-\x1f]/', $path) || strpos($path, '//') !== false || preg_match('~/(?:\.|\.\.)(?:/|$)~', $path)) ariaFail('invalid request');
    $path = rtrim($path, '/');
    if (strpos($path, $root.'/') !== 0) ariaFail('invalid request');
    $cursor = '';
    foreach (explode('/', ltrim($path, '/')) as $component) {
        $cursor .= '/'.$component;
        if (is_link($cursor)) ariaFail('invalid request');
    }
    if ($existing ? !is_dir($path) : (file_exists($path) || !is_dir(dirname($path)))) ariaFail('invalid request');
    $resolved = realpath($existing ? $path : dirname($path));
    if ($resolved === false || ($existing && $resolved !== $path) || (!$existing && $resolved !== dirname($path))) ariaFail('invalid request');
    return $path;
}
function ariaDeploymentOverlaps(string $a, string $b): bool {
    $a = rtrim($a, '/'); $b = rtrim($b, '/');
    // Unraid user-share and pool bind paths can address the same appdata.
    // Treat matching suffixes conservatively even when FUSE inode IDs differ.
    if (!ariaTest()) {
        $a = preg_replace('~^/mnt/[^/]+/appdata(?=/|$)~', '/mnt/user/appdata', $a);
        $b = preg_replace('~^/mnt/[^/]+/appdata(?=/|$)~', '/mnt/user/appdata', $b);
    }
    return $a === $b || strpos($a, $b.'/') === 0 || strpos($b, $a.'/') === 0;
}
function ariaDeploymentStoppedWriters(array $sources): void {
    foreach (ariaContainers() as $row) {
        if (in_array($row['State'] ?? '', ['exited', 'created', 'dead'], true)) continue;
        $info = ariaInspect(ariaName($row['Names'] ?? null, 'container'));
        foreach ($info['Mounts'] ?? [] as $mount) foreach ($sources as $source) {
            if (!empty($mount['RW']) && isset($mount['Source']) && ariaDeploymentOverlaps($source, $mount['Source'])) ariaFail('appdata in use');
        }
    }
}
/** Include directory/file metadata and content hashes; links, sockets and device files are rejected. */
function ariaDeploymentManifest(string $root, int $maxBytes): array {
    clearstatcache();
    $rootStat = lstat($root);
    if ($rootStat === false || is_link($root) || !is_dir($root)) ariaFail('unsupported appdata entry');
    $entries = ['.' => ['type' => 'directory', 'mode' => $rootStat['mode'] & 07777, 'uid' => $rootStat['uid'], 'gid' => $rootStat['gid']]];
    $bytes = 0; $deadline = microtime(true) + 300;
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::SELF_FIRST);
    foreach ($iterator as $file) {
        if (count($entries) >= 100000 || microtime(true) > $deadline) ariaFail('operation timed out');
        $path = $file->getPathname(); $relative = substr($path, strlen($root) + 1); $stat = lstat($path);
        if ($stat === false || is_link($path) || (!$file->isDir() && !$file->isFile())) ariaFail('unsupported appdata entry');
        $entry = ['type' => $file->isDir() ? 'directory' : 'file', 'mode' => $stat['mode'] & 07777, 'uid' => $stat['uid'], 'gid' => $stat['gid']];
        if ($file->isFile()) {
            $bytes += $stat['size']; if ($bytes > $maxBytes) ariaFail('copy budget exceeded');
            $entry['bytes'] = $stat['size']; $entry['sha256'] = hash_file('sha256', $path);
            if ($entry['sha256'] === false) ariaFail('command failed');
        }
        $entries[$relative] = $entry;
    }
    ksort($entries);
    return ['bytes' => $bytes, 'entries' => count($entries), 'sha256' => hash('sha256', ariaJson($entries))];
}
function ariaDeploymentRemoveCopy(string $path): void {
    if (is_link($path)) { if (!unlink($path)) ariaFail('command failed'); return; }
    if (!is_dir($path)) return;
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($path, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
    foreach ($iterator as $file) {
        $p = $file->getPathname();
        if ($file->isDir() && !$file->isLink()) { if (!rmdir($p)) ariaFail('command failed'); }
        elseif (!unlink($p)) ariaFail('command failed');
    }
    if (!rmdir($path)) ariaFail('command failed');
}
function ariaDeploymentCopy(string $source, string $destination, int $maxBytes): array {
    if ($maxBytes < 1 || $maxBytes > 1099511627776 || file_exists($destination) || is_link($destination)) ariaFail('invalid request');
    $before = ariaDeploymentManifest($source, $maxBytes);
    if (!mkdir($destination, 0700)) ariaFail('command failed');
    try {
        $copy = ariaRun([ariaPath('ARIA_AGENT_CP', '/bin/cp'), '-a', '--reflink=auto', '--', $source.'/.', $destination.'/'], 600);
        if ($copy['exit_code'] !== 0) ariaFail($copy['timed_out'] ? 'operation timed out' : 'command failed');
        $after = ariaDeploymentManifest($source, $maxBytes); $target = ariaDeploymentManifest($destination, $maxBytes);
        if ($before !== $after || $before !== $target) ariaFail('copy verification failed');
        return $target;
    } catch (Throwable $e) { ariaDeploymentRemoveCopy($destination); throw $e; }
}
function ariaDeploymentMigrate(string $state, string $jobId, array $a): array {
    $path = ariaTemplatePath($a['template']); ariaHash($path, $a['expected_sha256']);
    $xml = ariaReadFile($path, ARIA_MAX_XML); $tree = ariaXML($xml); $name = (string)$tree->Name;
    $source = ariaDeploymentAppdataPath($a['source']); $destination = ariaDeploymentAppdataPath($a['destination'], false);
    if (ariaDeploymentOverlaps($source, $destination) || ariaDeploymentOverlaps($state, $source) || ariaDeploymentOverlaps($state, $destination)) ariaFail('invalid request');
    $info = ariaInspect($name, false); if ($info !== null && !empty($info['State']['Running'])) ariaFail('appdata in use');
    $changed = 0;
    foreach ($tree->Config as $config) if (strtolower((string)$config['Type']) === 'path') {
        $value = (string)$config !== '' ? (string)$config : (string)$config['Default'];
        if ($value === $source || strpos($value, $source.'/') === 0) { $config[0] = $destination.substr($value, strlen($source)); $changed++; }
    }
    foreach ($tree->Data->Volume ?? [] as $volume) {
        $value = (string)$volume->HostDir;
        if ($value === $source || strpos($value, $source.'/') === 0) { $volume->HostDir = $destination.substr($value, strlen($source)); $changed++; }
    }
    if (!$changed) ariaFail('invalid request');
    ariaDeploymentStoppedWriters([$source]);
    $stage = dirname($destination).'/.aria-copy-'.$jobId;
    if (file_exists($stage) || is_link($stage)) ariaFail('invalid request');
    $copied = false;
    try {
        $manifest = ariaDeploymentCopy($source, $stage, $a['max_copy_bytes']); $copied = true;
        ariaDeploymentStoppedWriters([$source]); ariaHash($path, $a['expected_sha256']);
        if (file_exists($destination) || is_link($destination) || !rename($stage, $destination)) ariaFail('command failed');
        $copied = false;
        // If the final template write fails, the verified destination remains for
        // inspection. Never delete data after exposing the requested final path.
        try { $saved = ariaDeploymentWriteTemplate($state, $jobId, $a['template'], (string)$tree->asXML(), $a['expected_sha256']); }
        catch (Throwable $e) { return ['status' => 'partial', 'error' => ariaError($e), 'destination' => $destination, 'source_retained' => true, 'template_updated' => false, 'deployed' => false, 'verification' => $manifest]; }
        return $saved + ['name' => $name, 'source' => $source, 'destination' => $destination, 'source_retained' => true, 'template_updated' => true, 'changed_mounts' => $changed, 'verification' => $manifest];
    } finally { if ($copied) ariaDeploymentRemoveCopy($stage); }
}
function ariaDeploymentUpgradeTest(string $state, string $jobId, array $a): array {
    $path = ariaTemplatePath($a['template']); ariaHash($path, $a['expected_sha256']);
    $tree = ariaXML(ariaReadFile($path, ARIA_MAX_XML)); $name = (string)$tree->Name;
    if (!preg_match('/^[A-Za-z0-9][A-Za-z0-9._\/:@-]{0,511}$/D', $a['image'])) ariaFail('invalid request');
    if (strtolower((string)$tree->Privileged) === 'true' || strtolower((string)$tree->TailscaleEnabled) === 'true' || (string)$tree->ExtraParams !== '' || (string)$tree->PostArgs !== '' || count($tree->Device) > 0) ariaFail('unsupported template feature');
    foreach ($tree->Config as $config) if (strtolower((string)$config['Type']) === 'device') ariaFail('unsupported template feature');
    $mounts = ariaDeploymentMounts($tree); $sources = []; $targets = [];
    foreach ($mounts as $mount) {
        $source = ariaDeploymentAppdataPath($mount['source']);
        if (ariaDeploymentOverlaps($state, $source) || !preg_match('~^/[A-Za-z0-9_./ -]+$~D', $mount['target']) || strpos($mount['target'], '..') !== false || isset($targets[$mount['target']]) || !in_array($mount['mode'], ['ro', 'rw'], true)) ariaFail('unsupported template feature');
        $sources[$source] = true; $targets[$mount['target']] = true;
    }
    ariaDeploymentStoppedWriters(array_keys($sources));
    $production = ariaInspect($name, false); if ($production !== null && !empty($production['State']['Running'])) ariaFail('appdata in use');
    $image = ariaDocker(['image', 'inspect', $a['image']], 30, false);
    if ($image['exit_code'] !== 0) ariaDocker(['pull', $a['image']], 3600);
    $root = ariaDeploymentStore($state, 'upgrade-tests'); $directory = "$root/$jobId";
    if (file_exists($directory) || is_link($directory) || !mkdir($directory, 0700)) ariaFail('invalid request');
    $clone = 'aria-test-'.substr(hash('sha256', $jobId), 0, 24); $createdId = null; $retained = false; $result = null;
    try {
        $copies = []; $bytes = 0;
        foreach (array_keys($sources) as $index => $source) {
            if ($bytes >= $a['max_copy_bytes']) ariaFail('copy budget exceeded');
            $copy = "$directory/data-$index"; $manifest = ariaDeploymentCopy($source, $copy, $a['max_copy_bytes'] - $bytes); $bytes += $manifest['bytes']; $copies[$source] = $copy;
        }
        ariaDeploymentStoppedWriters(array_keys($sources)); ariaHash($path, $a['expected_sha256']);
        $environment = [];
        foreach ($tree->Config as $config) if (strtolower((string)$config['Type']) === 'variable') {
            $key = (string)$config['Target']; $value = (string)$config !== '' ? (string)$config : (string)$config['Default'];
            if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/D', $key) || preg_match('/[\r\n\x00]/', $value)) ariaFail('unsupported template feature');
            $environment[] = "$key=$value";
        }
        foreach ($tree->Environment->Variable ?? [] as $variable) {
            $key = (string)$variable->Name; $value = (string)$variable->Value;
            if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/D', $key) || preg_match('/[\r\n\x00]/', $value)) ariaFail('unsupported template feature');
            $environment[] = "$key=$value";
        }
        ariaAtomic("$directory/environment", implode("\n", $environment)."\n");
        $argv = ['create', '--name', $clone, '--network', 'none', '--restart', 'no', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true', '--pids-limit', '256', '--memory', '1g', '--cpus', '2', '--label', 'com.aria-gpt-bridge.test-job='.$jobId, '--env-file', "$directory/environment"];
        foreach ($mounts as $mount) $argv = array_merge($argv, ['--mount', 'type=bind,src='.$copies[$mount['source']].',dst='.$mount['target'].($mount['mode'] === 'ro' ? ',readonly' : '')]);
        $argv[] = $a['image'];
        $create = ariaDocker($argv, 120); $createdId = trim($create['output']);
        if (!preg_match('/^[a-f0-9]{64}$/D', $createdId)) { $createdId = null; ariaFail('command failed'); }
        $inspect = ariaInspect($createdId);
        if (($inspect['Config']['Labels']['com.aria-gpt-bridge.test-job'] ?? '') !== $jobId || ($inspect['HostConfig']['NetworkMode'] ?? '') !== 'none' || !empty($inspect['HostConfig']['Privileged']) || !empty($inspect['HostConfig']['Devices']) || !empty($inspect['HostConfig']['PortBindings'])) ariaFail('command failed');
        foreach ($inspect['Mounts'] ?? [] as $mount) if (($mount['Type'] ?? '') === 'bind' && !in_array($mount['Source'] ?? '', $copies, true)) ariaFail('command failed');
        ariaDocker(['start', $createdId], 120); $deadline = microtime(true) + $a['health_timeout_seconds'];
        do {
            $inspect = ariaInspect($createdId); $running = !empty($inspect['State']['Running']); $health = $inspect['State']['Health']['Status'] ?? null;
            if (!$running || $health === 'unhealthy' || $health === 'healthy' || microtime(true) >= $deadline) break;
            usleep(250000);
        } while (true);
        $result = ['name' => $name, 'image' => $a['image'], 'test_container' => $clone, 'status' => $running && $health === 'healthy' ? 'passed' : ($running && $health === null ? 'running_without_healthcheck' : 'failed'), 'health_status' => $health, 'application_health_verified' => $running && $health === 'healthy', 'copied_bytes' => $bytes, 'network' => 'none', 'production_changed' => false, 'limitations' => ['No external services or networking', 'No devices, capabilities or original host mounts', 'Running without a HEALTHCHECK does not establish application correctness']];
    } finally {
        if ($createdId === null) {
            // Docker can create successfully and lose its acknowledgement. Only
            // recover ownership using this unique job label, never the name alone.
            try {
                $candidate = ariaInspect($clone, false);
                if (($candidate['Config']['Labels']['com.aria-gpt-bridge.test-job'] ?? '') === $jobId) $createdId = $candidate['Id'];
            } catch (Throwable $e) { $retained = true; }
        }
        if ($createdId !== null) {
            // Immutable ID only. -v is safe here because these are clone-created
            // anonymous volumes, never production volumes or original mounts.
            $remove = ariaDocker(['rm', '--force', '--volumes', $createdId], 45, false);
            if ($remove['exit_code'] !== 0) $retained = true;
        }
        if (!$retained) ariaDeploymentRemoveCopy($directory);
        if ($result !== null) { $result['cleanup_complete'] = !$retained; $result['partial'] = $retained; $result['retained_directory'] = $retained ? $directory : null; }
    }
    return $result;
}
function ariaDeploymentRead(string $state, string $action, array $a): array {
    ariaDeploymentAuthorize($state, $action, $a);
    switch ($action) {
        case 'deployment_template_preview':
            $path = ariaTemplatePath($a['template']); ariaHash($path, $a['expected_sha256']);
            $old = ariaReadFile($path, ARIA_MAX_XML); $new = ariaRestoreXML($a['xml'], $old); $before = ariaDeploymentTemplateFields($old); $after = ariaDeploymentTemplateFields($new); $diff = [];
            foreach (array_unique(array_merge(array_keys($before), array_keys($after))) as $key) if (($before[$key] ?? null) !== ($after[$key] ?? null)) $diff[] = ['field' => $key, 'before' => $before[$key] ?? null, 'after' => $after[$key] ?? null];
            $oldSecrets = ariaRedactedXML($old)[1]; $newSecrets = ariaRedactedXML($new)[1];
            $secretChanges = 0;
            foreach (array_unique(array_merge(array_keys($oldSecrets), array_keys($newSecrets))) as $key) if (($oldSecrets[$key] ?? null) !== ($newSecrets[$key] ?? null)) $secretChanges++;
            return ['template' => $a['template'], 'current_sha256' => $a['expected_sha256'], 'proposed_sha256' => hash('sha256', $new), 'changes' => $diff, 'redacted_values_changed' => $secretChanges, 'redacted' => true, 'written' => false];
        case 'deployment_template_history': return ariaDeploymentHistory($state, $a['template']);
        case 'deployment_config_search':
            if (!is_string($a['query']) || strlen($a['query']) < 2 || strlen($a['query']) > 200) ariaFail('invalid request');
            $rows = [];
            foreach (ariaTemplateList() as $row) {
                try { ariaDeploymentAuthorize($state, 'template_get', ['template' => $row['template']]); } catch (Throwable $e) { continue; }
                $fields = ariaDeploymentTemplateFields(ariaReadFile(ariaTemplatePath($row['template']), ARIA_MAX_XML));
                foreach ($fields as $key => $value) if (stripos($key.' '.ariaJson($value), $a['query']) !== false) {
                    $rows[] = ['template' => $row['template'], 'name' => $row['name'], 'field' => $key, 'value' => $value];
                    if (count($rows) >= 200) return ['matches' => $rows, 'truncated' => true, 'redacted' => true];
                }
            }
            return ['matches' => $rows, 'truncated' => false, 'redacted' => true];
        case 'deployment_config_drift': return ariaDeploymentDrift($a['name']);
        case 'deployment_image_details': return ariaDeploymentImage($a['name']);
        case 'deployment_container_policy': return ariaDeploymentPolicy($state, $a['name']);
        case 'deployment_stacks':
            $rows = [];
            foreach (glob("$state/stacks/*.json") ?: [] as $path) { $stack = ariaDeploymentStack($state, basename($path, '.json')); $rows[] = $stack; }
            return ['stacks' => $rows];
        case 'deployment_stack_plan':
            $stack = ariaDeploymentStack($state, $a['name']); $order = ariaDeploymentStackOrder($stack['definition']);
            foreach ($order as &$member) { ariaDeploymentAuthorize($state, 'template_get', $member); $member['policy'] = ariaDeploymentPolicy($state, $member['name'])['policy']; } unset($member);
            return ['name' => $a['name'], 'sha256' => $stack['sha256'], 'order' => $order, 'runtime_preconditions_checked' => false];
    }
    ariaFail('unsupported action');
}
function ariaDeploymentExecute(string $state, array $job): array {
    $a = $job['arguments']; $id = ariaName($job['job_id'], 'id'); $action = $job['action']; ariaDeploymentAuthorize($state, $action, $a);
    switch ($action) {
        case 'deployment_template_restore':
            $history = ariaDeploymentHistory($state, $a['template']); $backupId = ariaName($a['backup_id'], 'id');
            if (!in_array($backupId, array_column($history['backups'], 'backup_id'), true)) ariaFail('not found');
            $xml = ariaReadFile(ariaFile("$state/backups", "$backupId.xml"), ARIA_MAX_XML);
            ariaDeploymentAuthorize($state, 'deployment_template_restore', ['template' => $a['template'], 'xml' => $xml]);
            return ariaDeploymentWriteTemplate($state, $id, $a['template'], $xml, $a['expected_sha256']);
        case 'deployment_image_pin':
            $path = ariaTemplatePath($a['template']); ariaHash($path, $a['expected_sha256']); $tree = ariaXML(ariaReadFile($path, ARIA_MAX_XML));
            $image = ariaDeploymentImage((string)$tree->Name);
            if (!in_array($a['digest'], $image['digests'], true)) ariaFail('invalid request');
            $tree->Repository = $a['digest'];
            return ariaDeploymentWriteTemplate($state, $id, $a['template'], (string)$tree->asXML(), $a['expected_sha256']) + ['image' => $a['digest']];
        case 'deployment_container_policy_save':
            return ariaDeploymentSaveJson($state, 'container-policies', $a['name'], ariaDeploymentPolicyCheck($a['policy']), $a['expected_sha256']);
        case 'deployment_stack_save':
            ariaDeploymentStackOrder($a['definition']);
            return ariaDeploymentSaveJson($state, 'stacks', $a['name'], $a['definition'], $a['expected_sha256']);
        case 'deployment_stack_apply':
            $stack = ariaDeploymentStack($state, $a['name']); if (!hash_equals($a['expected_sha256'], $stack['sha256'])) ariaFail('hash mismatch');
            $order = ariaDeploymentStackOrder($stack['definition']); $results = [];
            // Admission validates every member before any disruption. Re-check
            // again per member because earlier deployments may change conditions.
            foreach ($order as $member) ariaDeploymentAuthorize($state, 'template_deploy', $member);
            foreach ($order as $member) {
                try {
                    ariaDeploymentAuthorize($state, 'template_deploy', $member);
                    $result = ariaDeployment($state, $id, ['template' => $member['template'], 'expected_sha256' => $member['expected_sha256'], 'pull' => $a['pull'], 'start' => $a['start']]);
                    unset($result['output']); $results[] = $result;
                } catch (Throwable $e) { $results[] = ['name' => $member['name'], 'status' => 'failed', 'error' => ariaError($e)]; return ['name' => $a['name'], 'status' => count($results) > 1 ? 'partial' : 'failed', 'members' => $results, 'remaining_skipped' => count($order) - count($results)]; }
            }
            return ['name' => $a['name'], 'status' => 'succeeded', 'members' => $results, 'remaining_skipped' => 0];
        case 'deployment_upgrade_test': return ariaDeploymentUpgradeTest($state, $id, $a);
        case 'deployment_appdata_migrate': return ariaDeploymentMigrate($state, $id, $a);
    }
    ariaFail('unsupported action');
}
