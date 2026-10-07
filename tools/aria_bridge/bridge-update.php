<?php
/** Detached, fixed-source bridge updater. Never run through the ordinary worker. */
declare(strict_types=1);
require_once __DIR__.'/host-agent.php';

final class AriaBridgeUpdater {
    private const ORIGINS = [
        'https://github.com/kevinwaynekelly/kevin-cogs.git',
        'https://github.com/kevinwaynekelly/kevin-cogs',
        'git@github.com:kevinwaynekelly/kevin-cogs.git',
        'git@github.com:kevinwaynekelly/kevin-cogs',
        'ssh://git@github.com/kevinwaynekelly/kevin-cogs.git',
    ];
    private string $state;
    private string $path;
    private string $source;
    private string $ref;
    private array $job;
    private float $deadline;
    private ?array $original = null;
    private bool $upgradeAttempted = false;
    private bool $upgradeCompleted = false;

    public function __construct(string $state, string $jobId) {
        if ($state === '' || $state[0] !== '/' || !preg_match('/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/D', $jobId)) ariaFail('invalid request');
        $this->state = rtrim($state, '/');
        $this->path = $this->state.'/bridge-updates/'.$jobId.'.json';
        if (is_link($this->path) || !is_file($this->path)) ariaFail('invalid request');
        $this->source = (string)realpath(ariaPath('ARIA_BRIDGE_UPDATE_SOURCE', dirname(__DIR__, 2)));
        $this->ref = 'refs/aria-bridge-updates/'.$jobId;
        $this->deadline = microtime(true) + 1800;
    }

    private function save(): void { ariaAtomic($this->path, ariaJson($this->job)); }
    private function phase(string $phase): void { $this->job['phase'] = $phase; $this->save(); }
    private function fail(string $error): void { throw new DomainException($error); }
    private function command(array $argv, int $limit, string $error, bool $required = true): array {
        $remaining = (int)floor($this->deadline - microtime(true));
        if ($remaining < 1) $this->fail('bridge update deadline exceeded');
        $result = ariaRun($argv, min($limit, $remaining), $this->source ?: null);
        if ($result['exit_code'] !== 0) {
            // Provider output can contain credentials without recognizable
            // labels. Persist fixed command diagnostics, never that raw output.
            $this->job['diagnostics'][] = ['phase' => $this->job['phase'], 'operation' => $error, 'exit_code' => $result['exit_code'], 'timed_out' => $result['timed_out'], 'output_truncated' => $result['truncated']];
            $this->job['diagnostics'] = array_slice($this->job['diagnostics'], -12);
        }
        if ($required && $result['exit_code'] !== 0) $this->fail($error);
        return $result;
    }
    private function git(array $args, string $error = 'git operation failed', int $limit = 30, bool $required = true): array {
        return $this->command(array_merge([
            ariaPath('ARIA_BRIDGE_UPDATE_GIT', '/usr/bin/git'),
            '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'submodule.recurse=false', '-C', $this->source,
        ], $args), $limit, $error, $required);
    }
    private function text(array $args, string $error = 'git operation failed'): string {
        $result = $this->git($args, $error);
        if ($result['truncated']) $this->fail($error);
        return trim($result['output']);
    }
    private function head(): string {
        $revision = $this->text(['rev-parse', '--verify', 'HEAD^{commit}']);
        if (!preg_match('/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/D', $revision)) $this->fail('invalid source revision');
        return $revision;
    }
    private function clean(): bool {
        $result = $this->git(['status', '--porcelain=v1', '-z', '--untracked-files=all']);
        return !$result['truncated'] && $result['output'] === '';
    }
    private function assertCheckout(?string $expected = null): void {
        if ($this->source === '' || $this->text(['rev-parse', '--show-toplevel']) !== $this->source) $this->fail('bridge checkout unavailable');
        if ($this->text(['symbolic-ref', '--quiet', '--short', 'HEAD'], 'bridge checkout must use main') !== 'main') $this->fail('bridge checkout must use main');
        if (!in_array($this->text(['remote', 'get-url', '--all', 'origin']), self::ORIGINS, true)) $this->fail('unexpected bridge repository origin');
        if (!$this->clean()) $this->fail('bridge checkout has local changes');
        if ($expected !== null && $this->head() !== $expected) $this->fail('bridge checkout changed during update');
    }
    private function docker(array $args, string $error, int $limit = 30, bool $required = true): array {
        return $this->command(array_merge([ariaPath('ARIA_BRIDGE_UPDATE_DOCKER', '/usr/bin/docker')], $args), $limit, $error, $required);
    }
    private function inspect(string $id, bool $required = true): ?array {
        $format = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"running":{{json .State.Running}},"status":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},"update_id":{{json (index .Config.Labels "com.aria-gpt-bridge.update-id")}}}';
        $result = $this->docker(['inspect', '--type', 'container', '--format', $format, $id], 'bridge container unavailable', 20, $required);
        if ($result['exit_code'] !== 0) return null;
        if ($result['truncated']) $this->fail('invalid bridge container status');
        $value = json_decode(trim($result['output']), true);
        if (!is_array($value) || !preg_match('/^[a-f0-9]{64}$/D', $value['id'] ?? '') || !preg_match('/^sha256:[a-f0-9]{64}$/D', $value['image'] ?? '') || !is_bool($value['running'] ?? null)) $this->fail('invalid bridge container status');
        return $value;
    }
    private function service(string $action): void {
        $this->command(['/bin/bash', ariaPath('ARIA_BRIDGE_UPDATE_SERVICE', $this->source.'/tools/aria_bridge/host-service.sh'), $action], 130, 'host service recovery failed');
    }

    private function restoreContainer(): void {
        if ($this->original === null) $this->fail('original bridge container is unknown');
        $old = $this->inspect($this->original['id'], false);
        if ($old === null || $old['image'] !== $this->original['image']) $this->fail('original bridge container is unavailable');
        $current = $this->inspect('aria-gpt-bridge', false);
        if (($current['id'] ?? null) !== $old['id']) {
            if (!preg_match('~^/aria-gpt-bridge-rollback-[0-9]+-[0-9]+$~D', $old['name'] ?? '')) $this->fail('original bridge container was renamed externally');
            if ($current !== null) {
                if (($current['update_id'] ?? null) !== $this->job['job_id']) $this->fail('bridge container changed during update');
                $this->docker(['rm', '-f', $current['id']], 'failed bridge container could not be removed', 30);
            }
            $this->docker(['rename', $old['id'], 'aria-gpt-bridge'], 'original bridge container could not be restored');
        }
        if ($this->original['running']) {
            $this->docker(['start', $old['id']], 'original bridge container could not be started', 30);
            for ($attempt = 0; $attempt < 100; $attempt++) {
                $current = $this->inspect('aria-gpt-bridge');
                if ($current['id'] !== $old['id']) $this->fail('bridge container changed during recovery');
                if ($current['running'] && $current['health'] === 'healthy') return;
                if (in_array($current['status'], ['dead', 'exited'], true)) break;
                sleep(1);
            }
            $this->fail('restored bridge did not become healthy');
        }
        $current = $this->inspect('aria-gpt-bridge');
        if ($current['running']) $this->docker(['stop', '--time', '20', $old['id']], 'original stopped bridge could not be restored', 30);
    }

    private function rollback(): array {
        $this->deadline = microtime(true) + 300;
        $result = ['status' => 'incomplete', 'source_restored' => false, 'host_restarted' => false, 'container_restored' => false];
        $this->phase('rollback');
        try {
            $from = $this->job['from_revision'];
            $target = $this->job['to_revision'];
            // Refuse to reset a moved branch or dirty tree. --keep provides an
            // additional Git guard against edits arriving after this check.
            $this->assertCheckout();
            $head = $this->head();
            if ($head !== $from && $head !== $target) $this->fail('bridge checkout changed during update');
            if ($head !== $from) {
                $this->git(['reset', '--keep', $from], 'source revision could not be restored');
                $this->assertCheckout($from);
            }
            $result['source_restored'] = true;
            if ($this->upgradeAttempted) {
                $this->service('restart');
                $this->service('install-boot');
                $result['host_restarted'] = true;
                // Recover by original immutable ID even if timeout killed the
                // shell installer's EXIT trap before it could finish rollback.
                $this->restoreContainer();
                $result['container_restored'] = true;
            }
            $result['status'] = 'complete';
        } catch (Throwable $e) {
            $result['error'] = $e instanceof DomainException ? $e->getMessage() : 'bridge recovery failed';
        }
        return $result;
    }

    public function run(): int {
        // CLOEXEC prevents upgrade children or new supervisors retaining this
        // liveness lock after the detached updater itself exits.
        $lock = ariaLock($this->state, 'bridge-update-runner', true);
        try {
            $this->job = ariaReadJson($this->path);
            if (($this->job['action'] ?? '') !== 'bridge_update' || ($this->job['status'] ?? '') !== 'queued') return 1;
            $stat = (string)file_get_contents('/proc/self/stat');
            $fields = preg_split('/\s+/', trim(substr($stat, (int)strrpos($stat, ')') + 1)));
            $this->job['pid'] = getmypid();
            $this->job['start_time'] = $fields[19] ?? '';
            $this->job['status'] = 'running';
            $this->job['started_at'] = ariaNow();
            $this->job['finished_at'] = null;
            $this->job['error'] = null;
            $this->job['from_revision'] = null;
            $this->job['to_revision'] = null;
            $this->phase('preflight');
            unset($GLOBALS['ARIA_WORKER_STATE']);
            putenv('GIT_TERMINAL_PROMPT=0');
            putenv('ARIA_APPDATA_ROOT='.dirname($this->state));
            putenv('ARIA_BRIDGE_UPDATE_ID='.$this->job['job_id']);
            try {
                // The installer checks our runner lock after taking install.lock.
                // This opposite nonblocking check rejects an installer that was
                // already active, without passing inherited lock FDs to children.
                $installPath = dirname($this->state).'/install.lock';
                if (is_link($installPath)) $this->fail('bridge installer lock is invalid');
                $installLock = fopen($installPath, 'ce');
                if ($installLock === false || !flock($installLock, LOCK_EX | LOCK_NB)) $this->fail('another bridge installer is running');
                flock($installLock, LOCK_UN);
                fclose($installLock);
                $this->assertCheckout();
                $from = $this->head();
                $this->job['from_revision'] = $from;
                $this->phase('fetch');
                $this->git(['fetch', '--no-tags', '--no-recurse-submodules', 'origin', 'refs/heads/main:'.$this->ref], 'bridge fetch failed', 120);
                $target = $this->text(['rev-parse', '--verify', $this->ref.'^{commit}']);
                if (!preg_match('/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/D', $target)) $this->fail('invalid target revision');
                $this->job['to_revision'] = $target;
                $this->save();
                $this->assertCheckout($from);
                if ($this->git(['merge-base', '--is-ancestor', $from, $target], 'bridge history diverged', 30, false)['exit_code'] !== 0) $this->fail('bridge history diverged');
                $this->original = $this->inspect('aria-gpt-bridge');
                $this->job['original_container'] = $this->original;
                $this->phase('checkout');
                $this->git(['merge', '--ff-only', '--no-edit', $target], 'source fast-forward failed');
                $this->assertCheckout($target);
                $this->phase('upgrade');
                $this->upgradeAttempted = true;
                $this->command(['/bin/bash', ariaPath('ARIA_BRIDGE_UPDATE_UPGRADE', $this->source.'/tools/aria_bridge/upgrade-unraid.sh')], 1500, 'bridge upgrade failed');
                $this->upgradeCompleted = true;
                $this->phase('verify');
                $this->assertCheckout($target);
                $current = $this->inspect('aria-gpt-bridge');
                if (!$current['running'] || $current['health'] !== 'healthy' || ($current['update_id'] ?? null) !== $this->job['job_id']) $this->fail('bridge final health verification failed');
                $this->job['status'] = 'succeeded';
                $this->job['phase'] = 'complete';
                $this->job['result'] = ['updated' => true, 'bridge_healthy' => true, 'source_revision' => $target];
            } catch (Throwable $e) {
                $this->job['error'] = $e instanceof DomainException ? $e->getMessage() : 'bridge update failed';
                $hasTarget = is_string($this->job['from_revision']) && is_string($this->job['to_revision']);
                if ($hasTarget && !$this->upgradeCompleted) {
                    $recovery = $this->rollback();
                    $this->job['result'] = ['updated' => false, 'rollback' => $recovery];
                    $this->job['status'] = $recovery['status'] === 'complete' ? 'failed' : 'unknown';
                } elseif ($this->upgradeCompleted) {
                    // A completed upgrade followed by concurrent checkout edits
                    // needs review, not a reset or removal of a working service.
                    $this->job['status'] = 'unknown';
                    $this->job['result'] = ['updated' => true, 'rollback' => ['status' => 'skipped', 'error' => 'upgrade completed; original container may no longer exist']];
                } else {
                    $this->job['status'] = 'failed';
                    $this->job['result'] = ['updated' => false];
                }
                $this->job['phase'] = $this->job['status'] === 'unknown' ? 'manual_review' : 'failed';
            }
            $this->job['finished_at'] = ariaNow();
            $this->save();
            return $this->job['status'] === 'succeeded' ? 0 : 1;
        } finally {
            ariaUnlock($lock);
        }
    }
}

if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    ini_set('display_errors', '0');
    try {
        if (count($argv) !== 3 || (!ariaTest() && posix_geteuid() !== 0)) ariaFail('invalid request');
        exit((new AriaBridgeUpdater($argv[1], $argv[2]))->run());
    } catch (Throwable $e) {
        fwrite(STDERR, "Aria bridge update runner could not start.\n");
        exit(1);
    }
}
