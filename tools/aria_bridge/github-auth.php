<?php
/** Host-only GitHub authentication. Never evaluate the shared file as shell code. */
declare(strict_types=1);

function ariaGithubAuthentication(): array {
    $environment = getenv();
    $environment = is_array($environment) ? $environment : [];
    $file = ariaPath('ARIA_GITHUB_CREDENTIALS', '/boot/config/plugins/user.scripts/credentials.conf');
    if (!ariaTest() || getenv('ARIA_GITHUB_CREDENTIALS') !== false) {
        if (is_link($file)) ariaFail('GitHub credentials file is invalid');
        if (file_exists($file)) {
            if (!is_file($file) || (!ariaTest() && fileowner($file) !== 0) || (fileperms($file) & 0077) !== 0) ariaFail('GitHub credentials file must be root-private');
            foreach (explode("\n", ariaReadFile($file, 131072)) as $line) {
                if (!preg_match('/^\s*(?:export\s+)?(GITHUB_TOKEN|GH_CONFIG_DIR)\s*=(.*)$/D', $line, $match)) continue;
                $raw = trim($match[2]);
                if (preg_match("/^'([^']*)'\\s*(?:#.*)?$/D", $raw, $value) || preg_match('/^"([^"$`\\\\]*)"\s*(?:#.*)?$/D', $raw, $value) || preg_match('/^([A-Za-z0-9_\/.:+~-]*)\s*(?:#.*)?$/D', $raw, $value)) $literal = $value[1];
                else ariaFail('GitHub settings must use literal assignments');
                if (preg_match('/[\x00-\x1f\x7f]/', $literal)) ariaFail('GitHub settings are invalid');
                if ($match[1] === 'GH_CONFIG_DIR' && $literal !== '' && $literal[0] !== '/') ariaFail('GitHub configuration directory must be absolute');
                if ($literal !== '') $environment[$match[1] === 'GITHUB_TOKEN' ? 'GH_TOKEN' : 'GH_CONFIG_DIR'] = $literal;
            }
        }
    }
    $cli = ariaTest() ? (getenv('ARIA_GITHUB_CLI') ?: '') : '/mnt/user/appdata/github-cli/bin/gh';
    if (!ariaTest() && !is_executable($cli)) $cli = '/usr/bin/gh';
    if ($cli === '' || !is_file($cli) || !is_executable($cli)) $cli = null;
    if ($cli === null && !empty($environment['GH_TOKEN'])) ariaFail('GitHub CLI is required for saved token authentication');
    $environment['GIT_TERMINAL_PROMPT'] = '0';
    $environment['GH_PROMPT_DISABLED'] = '1';
    return ['cli' => $cli, 'environment' => $environment];
}
function ariaGithubGitOptions(array $auth): array {
    if ($auth['cli'] === null) return ['-c', 'credential.interactive=false'];
    // Git interprets ! helpers as shell commands. Quote only the trusted executable path.
    return ['-c', 'credential.interactive=false', '-c', 'credential.helper=', '-c', 'credential.helper=!'.escapeshellarg($auth['cli']).' auth git-credential'];
}
function ariaGithubChecks(string $repository, string $revision): array {
    if (!preg_match('~^kevinwaynekelly/(?:aria-gpt-bridge|kevin-cogs)$~D', $repository) || !preg_match('/^[a-f0-9]{40}(?:[a-f0-9]{24})?$/D', $revision)) ariaFail('invalid request');
    $auth = ariaGithubAuthentication();
    $endpoint = "repos/$repository/commits/$revision/check-runs?per_page=100";
    if ($auth['cli'] !== null) return ariaRun([$auth['cli'], 'api', '--hostname', 'github.com', '--method', 'GET', $endpoint], 40, null, $auth['environment']);
    return ariaRun([ariaPath('ARIA_AUTOMATION_CURL', '/usr/bin/curl'), '--fail', '--silent', '--show-error', '--max-time', '30', '--max-filesize', '524288', '--proto', '=https', '--header', 'Accept: application/vnd.github+json', '--header', 'User-Agent: aria-bridge-updater', 'https://api.github.com/'.$endpoint], 40);
}

// Internal installer entry point. Provider output is never included in failure diagnostics.
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    require_once __DIR__.'/host-agent.php';
    try {
        if (($argv[1] ?? '') !== 'git' || count($argv) < 3) ariaFail('invalid request');
        $auth = ariaGithubAuthentication();
        $result = ariaRun(array_merge([ariaPath('ARIA_GITHUB_GIT', '/usr/bin/git'), '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false'], ariaGithubGitOptions($auth), array_slice($argv, 2)), 180, null, $auth['environment']);
        if ($result['exit_code'] !== 0 || $result['truncated']) ariaFail('Authenticated Git operation failed; check the saved GitHub login and repository access');
        exit(0);
    } catch (Throwable $error) { fwrite(STDERR, ariaError($error)."\n"); exit(1); }
}
