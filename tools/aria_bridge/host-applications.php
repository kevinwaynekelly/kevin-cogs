<?php
/** Typed application APIs. Credentials stay in the host-only operations vault.
 * API sources: https://sonarr.tv/docs/api/ https://radarr.video/docs/api/
 * https://developer.plex.tv/pms/ and qBittorrent's official WebUI API wiki.
 * Provider replies are data, never URLs or commands to follow.
 */
declare(strict_types=1);

const ARIA_APPLICATIONS_MAX_RESPONSE = 524288;
const ARIA_APPLICATIONS_TYPES = ['sonarr', 'radarr', 'plex', 'qbittorrent'];

function ariaApplicationsName($name): string {
    if (!is_string($name) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/D', $name)) ariaFail('invalid request');
    return $name;
}
function ariaApplicationsDirectory(string $state): string {
    $dir = $state.'/applications';
    if (is_link($dir)) ariaFail('invalid request');
    if (!is_dir($dir) && !mkdir($dir, 0700)) ariaFail('internal error');
    chmod($dir, 0700);
    return $dir;
}
function ariaApplicationsPath(string $state, string $profile, bool $required = true): string {
    return ariaFile(ariaApplicationsDirectory($state), ariaApplicationsName($profile).'.json', $required);
}
function ariaApplicationsURL($url): string {
    if (!is_string($url) || strlen($url) > 1024 || preg_match('/[\x00-\x20\x7f\\\\]/', $url)) ariaFail('invalid request');
    $parts = parse_url($url);
    if (!is_array($parts) || !in_array($parts['scheme'] ?? '', ['http', 'https'], true)
        || empty($parts['host']) || isset($parts['user']) || isset($parts['pass'])
        || isset($parts['query']) || isset($parts['fragment'])) ariaFail('invalid request');
    if (!preg_match('/^(?:[A-Za-z0-9_.-]+|\[[a-fA-F0-9:]+\])$/D', $parts['host'])) ariaFail('invalid request');
    $path = $parts['path'] ?? '';
    if (!preg_match('~^(?:/[A-Za-z0-9_-]+)*/*$~D', $path)) ariaFail('invalid request');
    return rtrim($url, '/');
}
function ariaApplicationsConfig(array $a): array {
    $type = $a['type'] ?? null;
    if (!in_array($type, ARIA_APPLICATIONS_TYPES, true)) ariaFail('invalid request');
    $verify = $a['verify_tls'] ?? true;
    $version = $a['api_version'] ?? 'v3';
    if (!is_bool($verify) || !in_array($version, ['v3', 'v5'], true) || ($version === 'v5' && $type !== 'sonarr')) ariaFail('invalid request');
    return ['profile' => ariaApplicationsName($a['profile'] ?? null), 'type' => $type,
        'base_url' => ariaApplicationsURL($a['base_url'] ?? null),
        'secret_ref' => ariaApplicationsName($a['secret_ref'] ?? null),
        'verify_tls' => $verify, 'api_version' => $version];
}
function ariaApplicationsProfile(string $state, string $name, ?string $expected = null): array {
    $path = ariaApplicationsPath($state, $name);
    $bytes = ariaReadFile($path, 16384);
    $value = json_decode($bytes, true);
    if (!is_array($value)) ariaFail('invalid request');
    $profile = ariaApplicationsConfig($value);
    if ($profile['profile'] !== $name) ariaFail('invalid request');
    $profile['sha256'] = hash('sha256', $bytes);
    if ($expected !== null && (!preg_match('/^[a-f0-9]{64}$/D', $expected) || !hash_equals($expected, $profile['sha256']))) ariaFail('hash mismatch');
    return $profile;
}
function ariaApplicationsOperations(string $type): array {
    if ($type === 'sonarr' || $type === 'radarr') return ['read' => ['status', 'health', 'queue', 'history', 'missing', 'commands', 'library'], 'write' => ['refresh', 'rescan', 'search', 'rss_sync']];
    if ($type === 'plex') return ['read' => ['status', 'sessions', 'library'], 'write' => ['rescan']];
    return ['read' => ['status', 'downloads'], 'write' => ['pause', 'resume', 'recheck', 'reannounce']];
}
function ariaApplicationsCredential(string $state, array $profile): array {
    if (!function_exists('ariaOperationsSecretValue')) ariaFail('application credential unavailable');
    try { $value = ariaOperationsSecretValue($state, $profile['secret_ref']); }
    catch (Throwable $e) { ariaFail('application credential unavailable'); }
    if ($profile['type'] === 'qbittorrent') {
        $auth = json_decode($value, true);
        if (!is_array($auth) || !is_string($auth['username'] ?? null) || !is_string($auth['password'] ?? null)
            || $auth['username'] === '' || $auth['password'] === '' || strlen($auth['username']) > 1024 || strlen($auth['password']) > 4096) ariaFail('application credential unavailable');
        return ['username' => $auth['username'], 'password' => $auth['password']];
    }
    $value = trim($value);
    if ($value === '' || strlen($value) > 8192 || preg_match('/[\x00-\x20\x7f]/', $value)) ariaFail('application credential unavailable');
    return ['token' => $value];
}
/** Bounded HTTP, no redirects, no external commands, no credentials in URLs. */
function ariaApplicationsHTTP(array $profile, string $method, string $path, array $headers, string $body = ''): array {
    if (!in_array($method, ['GET', 'POST'], true) || $path === '' || $path[0] !== '/' || strpos($path, '://') !== false || strpos($path, "\r") !== false || strpos($path, "\n") !== false) ariaFail('invalid request');
    $url = ariaApplicationsURL($profile['base_url']).$path;
    $remaining = min(5.0, ($GLOBALS['ARIA_APPLICATIONS_DEADLINE'] ?? microtime(true) + 20) - microtime(true));
    if ($remaining <= 0) ariaFail('operation timed out');
    if (parse_url($url, PHP_URL_SCHEME) === 'https' && !extension_loaded('openssl')) ariaFail('application transport unavailable');
    foreach ($headers as $header) if (!is_string($header) || preg_match('/[\r\n]/', $header)) ariaFail('invalid request');
    $headers = array_merge(['Accept: application/json', 'Accept-Encoding: identity', 'Connection: close'], $headers);
    $context = stream_context_create([
        'http' => ['method' => $method, 'header' => implode("\r\n", $headers), 'content' => $body,
            'timeout' => $remaining, 'ignore_errors' => true, 'follow_location' => 0,
            'max_redirects' => 0, 'protocol_version' => 1.1],
        'ssl' => ['verify_peer' => $profile['verify_tls'], 'verify_peer_name' => $profile['verify_tls'], 'allow_self_signed' => !$profile['verify_tls']],
    ]);
    $deadline = microtime(true) + $remaining;
    $handle = @fopen($url, 'rb', false, $context);
    if ($handle === false) ariaFail('application connection failed');
    $reply = '';
    try {
        $meta = stream_get_meta_data($handle);
        $responseHeaders = $meta['wrapper_data'] ?? [];
        $status = 0;
        foreach ($responseHeaders as $header) if (preg_match('~^HTTP/\S+\s+(\d{3})~', $header, $match)) $status = (int)$match[1];
        if ($status >= 300 && $status < 400) ariaFail('application redirect refused');
        if ($status === 401 || $status === 403) ariaFail('application authentication failed');
        if ($status === 404 || $status === 405) ariaFail('application operation unavailable');
        if ($status < 200 || $status >= 300) ariaFail('application request failed');
        while (!feof($handle)) {
            $remaining = $deadline - microtime(true);
            if ($remaining <= 0) ariaFail('operation timed out');
            stream_set_timeout($handle, (int)$remaining, (int)(($remaining - floor($remaining)) * 1000000));
            $chunk = fread($handle, 8192);
            $meta = stream_get_meta_data($handle);
            if (!empty($meta['timed_out'])) ariaFail('operation timed out');
            if ($chunk === false) ariaFail('application connection failed');
            $reply .= $chunk;
            if (strlen($reply) > ARIA_APPLICATIONS_MAX_RESPONSE) ariaFail('application response too large');
        }
        return ['status' => $status, 'body' => $reply, 'headers' => $responseHeaders];
    } finally { fclose($handle); }
}
function ariaApplicationsJSON(array $reply): array {
    $value = json_decode($reply['body'], true, 32);
    if (!is_array($value)) ariaFail('application invalid response');
    return $value;
}
/** Only project necessary fields. Do not emit raw tracker URLs, config or tokens. */
function ariaApplicationsClean($value, array $secrets, int $depth = 0) {
    if ($depth > 6) return '[depth limited]';
    if (is_string($value)) {
        foreach ($secrets as $secret) if (is_string($secret) && $secret !== '') $value = str_replace($secret, '[REDACTED]', $value);
        $value = preg_replace('~https?://[^\s<>"\x27]+~i', '[URL omitted]', $value);
        return ariaScrub($value, [], 1024);
    }
    if (!is_array($value)) return is_scalar($value) || $value === null ? $value : null;
    $out = [];
    foreach (array_slice($value, 0, 200, true) as $key => $item) {
        if (is_string($key) && preg_match('/password|passwd|token|secret|credential|api.?key|authorization|cookie|tracker|magnet|downloadurl/i', $key)) continue;
        $out[$key] = ariaApplicationsClean($item, $secrets, $depth + 1);
    }
    return $out;
}
function ariaApplicationsProject(array $row, array $fields, array $secrets): array {
    return ariaApplicationsClean(array_intersect_key($row, array_flip($fields)), $secrets);
}
function ariaApplicationsRows(array $rows, array $fields, array $secrets, int $limit = 50): array {
    $out = []; $size = 0;
    foreach ($rows as $row) {
        if (!is_array($row)) continue;
        if (count($out) >= $limit) break;
        $item = ariaApplicationsProject($row, $fields, $secrets);
        $size += strlen(ariaJson($item));
        if ($size > 196608) break;
        $out[] = $item;
    }
    return ['items' => $out, 'returned' => count($out), 'truncated' => count($out) < count($rows)];
}
function ariaApplicationsInt($value, int $default, int $max): int {
    if ($value === null) return $default;
    if (!is_int($value) || $value < 1 || $value > $max) ariaFail('invalid request');
    return $value;
}
function ariaApplicationsPlexData(array $reply): array {
    $value = json_decode($reply['body'], true, 32);
    if (is_array($value) && isset($value['MediaContainer']) && is_array($value['MediaContainer'])) return $value['MediaContainer'];
    if (preg_match('/<!DOCTYPE|<!ENTITY/i', $reply['body']) || !function_exists('simplexml_load_string')) ariaFail('application invalid response');
    libxml_use_internal_errors(true);
    $xml = simplexml_load_string($reply['body'], 'SimpleXMLElement', LIBXML_NONET);
    libxml_clear_errors();
    if ($xml === false || $xml->getName() !== 'MediaContainer') ariaFail('application invalid response');
    $value = [];
    foreach ($xml->attributes() as $key => $item) $value[$key] = (string)$item;
    foreach (['Directory', 'Video', 'Track', 'Photo'] as $tag) foreach ($xml->$tag as $child) {
        $item = [];
        foreach ($child->attributes() as $key => $attr) $item[$key] = (string)$attr;
        foreach (['Player', 'Session', 'TranscodeSession'] as $subtag) if (isset($child->$subtag)) {
            $item[$subtag] = [];
            foreach ($child->$subtag->attributes() as $key => $attr) $item[$subtag][$key] = (string)$attr;
        }
        $value[$tag][] = $item;
    }
    return $value;
}
function ariaApplicationsAuth(array $profile, array $auth): array {
    if ($profile['type'] === 'sonarr' || $profile['type'] === 'radarr') return ['X-Api-Key: '.$auth['token']];
    if ($profile['type'] === 'plex') return ['X-Plex-Token: '.$auth['token'], 'X-Plex-Client-Identifier: aria-gpt-bridge', 'X-Plex-Product: Aria'];
    $parts = parse_url($profile['base_url']);
    $origin = $parts['scheme'].'://'.$parts['host'].(isset($parts['port']) ? ':'.$parts['port'] : '');
    $headers = ['Referer: '.$profile['base_url'].'/', 'Origin: '.$origin];
    $reply = ariaApplicationsHTTP($profile, 'POST', '/api/v2/auth/login', array_merge($headers, ['Content-Type: application/x-www-form-urlencoded']), http_build_query($auth, '', '&', PHP_QUERY_RFC3986));
    if (trim($reply['body']) !== 'Ok.') ariaFail('application authentication failed');
    $cookie = null;
    foreach ($reply['headers'] as $header) if (preg_match('/^Set-Cookie:\s*SID=([A-Za-z0-9_-]{1,256})(?:;|$)/i', $header, $match)) $cookie = $match[1];
    if ($cookie === null) ariaFail('application authentication failed');
    $headers[] = 'Cookie: SID='.$cookie;
    return $headers;
}
function ariaApplicationsQuery(string $state, array $profile, array $a): array {
    $operation = $a['operation'] ?? '';
    if (!in_array($operation, ariaApplicationsOperations($profile['type'])['read'], true)) ariaFail('application operation unavailable');
    $limit = ariaApplicationsInt($a['limit'] ?? null, 50, 200);
    $page = ariaApplicationsInt($a['page'] ?? null, 1, 100000);
    $itemId = isset($a['item_id']) ? ariaApplicationsInt($a['item_id'], 1, 2147483647) : null;
    $torrentHash = $a['torrent_hash'] ?? null;
    if ($torrentHash !== null) {
        ariaApplicationsIDs([$torrentHash], true);
        if ($profile['type'] !== 'qbittorrent' || $operation !== 'downloads') ariaFail('invalid request');
    }
    $auth = ariaApplicationsCredential($state, $profile);
    $headers = ariaApplicationsAuth($profile, $auth);
    $secrets = array_values($auth);
    foreach ($headers as $header) if (strpos($header, 'Cookie: SID=') === 0) $secrets[] = substr($header, 12);
    try {
        $type = $profile['type'];
        if ($type === 'sonarr' || $type === 'radarr') {
            $paths = ['status' => 'system/status', 'health' => 'health', 'queue' => 'queue', 'history' => 'history', 'missing' => 'wanted/missing', 'commands' => 'command', 'library' => $type === 'sonarr' ? 'series' : 'movie'];
            $path = '/api/'.$profile['api_version'].'/'.$paths[$operation];
            if ($itemId !== null) {
                if (!in_array($operation, ['library', 'commands'], true)) ariaFail('invalid request');
                $path .= '/'.$itemId;
            }
            if (in_array($operation, ['queue', 'history', 'missing'], true)) $path .= '?'.http_build_query(['page' => $page, 'pageSize' => $limit, 'includeUnknownSeriesItems' => 'true', 'includeUnknownMovieItems' => 'true']);
            $value = ariaApplicationsJSON(ariaApplicationsHTTP($profile, 'GET', $path, $headers));
            if ($operation === 'status') return ariaApplicationsProject($value, ['appName', 'instanceName', 'version', 'buildTime', 'isDebug', 'isProduction', 'isAdmin', 'isDocker', 'runtimeVersion', 'startupPath', 'appData', 'osName', 'osVersion', 'startTime', 'migrationVersion'], $secrets);
            $fields = ['id', 'title', 'path', 'status', 'trackedDownloadStatus', 'trackedDownloadState', 'statusMessages', 'downloadId', 'downloadClient', 'protocol', 'size', 'sizeleft', 'timeleft', 'estimatedCompletionTime', 'added', 'downloadForced', 'eventType', 'date', 'sourceTitle', 'movieId', 'seriesId', 'episodeId', 'episodeNumber', 'seasonNumber', 'airDateUtc', 'monitored', 'hasFile', 'hasMovieFile', 'type', 'message', 'source', 'name', 'state', 'queued', 'started', 'ended', 'duration', 'completionMessage', 'quality', 'year'];
            $rows = isset($value['records']) && is_array($value['records']) ? $value['records'] : ($itemId !== null ? [$value] : $value);
            $result = ariaApplicationsRows($rows, $fields, $secrets, $limit);
            if (isset($value['totalRecords'])) { $result['total_records'] = (int)$value['totalRecords']; $result['page'] = $page; }
            return $result;
        }
        if ($type === 'plex') {
            if ($itemId !== null && $operation !== 'library') ariaFail('invalid request');
            $path = ['status' => '/', 'sessions' => '/status/sessions', 'library' => '/library/sections'][$operation];
            if ($itemId !== null) $path = '/library/sections/'.$itemId.'/all?X-Plex-Container-Start='.(($page - 1) * $limit).'&X-Plex-Container-Size='.$limit;
            $value = ariaApplicationsPlexData(ariaApplicationsHTTP($profile, 'GET', $path, $headers));
            if ($operation === 'status') return ariaApplicationsProject($value, ['friendlyName', 'version', 'platform', 'platformVersion', 'updatedAt', 'myPlexSigninState', 'transcoderActiveVideoSessions', 'size'], $secrets);
            $rows = [];
            foreach (['Directory', 'Metadata', 'Video', 'Track', 'Photo'] as $key) if (isset($value[$key]) && is_array($value[$key])) $rows = array_merge($rows, $value[$key]);
            $fields = ['key', 'ratingKey', 'type', 'title', 'grandparentTitle', 'parentTitle', 'librarySectionID', 'viewOffset', 'duration', 'year', 'updatedAt', 'refreshing', 'scanner', 'language', 'Player', 'Session', 'TranscodeSession'];
            foreach ($rows as &$row) {
                if (!is_array($row)) continue;
                foreach (['Player' => ['state', 'platform', 'product', 'local'], 'Session' => ['bandwidth', 'location'], 'TranscodeSession' => ['progress', 'speed', 'throttled', 'videoDecision', 'audioDecision']] as $key => $allow) {
                    if (isset($row[$key]) && is_array($row[$key])) $row[$key] = array_intersect_key($row[$key], array_flip($allow));
                }
            }
            unset($row);
            $result = ariaApplicationsRows($rows, $fields, $secrets, $limit);
            $result['total_records'] = (int)($value['totalSize'] ?? $value['size'] ?? count($rows));
            return $result;
        }
        if ($itemId !== null) ariaFail('invalid request');
        if ($operation === 'status') {
            $version = ariaApplicationsHTTP($profile, 'GET', '/api/v2/app/version', $headers);
            $value = ariaApplicationsJSON(ariaApplicationsHTTP($profile, 'GET', '/api/v2/transfer/info', $headers));
            return ['version' => ariaApplicationsClean(trim($version['body']), $secrets)] + ariaApplicationsProject($value, ['dl_info_speed', 'dl_info_data', 'up_info_speed', 'up_info_data', 'dl_rate_limit', 'up_rate_limit', 'dht_nodes', 'connection_status'], $secrets);
        }
        $query = ['limit' => $limit, 'offset' => ($page - 1) * $limit, 'sort' => 'added_on', 'reverse' => 'true'];
        if ($torrentHash !== null) $query['hashes'] = $torrentHash;
        $value = ariaApplicationsJSON(ariaApplicationsHTTP($profile, 'GET', '/api/v2/torrents/info?'.http_build_query($query), $headers));
        return ariaApplicationsRows($value, ['hash', 'name', 'state', 'progress', 'size', 'total_size', 'amount_left', 'downloaded', 'uploaded', 'dlspeed', 'upspeed', 'eta', 'ratio', 'num_seeds', 'num_leechs', 'category', 'tags', 'save_path', 'content_path', 'added_on', 'completion_on', 'last_activity'], $secrets, $limit) + ['page' => $page, 'more_possible' => count($value) >= $limit];
    } finally {
        if ($profile['type'] === 'qbittorrent') {
            try { ariaApplicationsHTTP($profile, 'POST', '/api/v2/auth/logout', $headers); } catch (Throwable $e) { }
        }
    }
}
function ariaApplicationsIDs($values, bool $hashes = false): array {
    if (!is_array($values) || count($values) < 1 || count($values) > 100 || array_keys($values) !== range(0, count($values) - 1)) ariaFail('invalid request');
    foreach ($values as $value) {
        if ($hashes) { if (!is_string($value) || !preg_match('/^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$/D', $value)) ariaFail('invalid request'); }
        else ariaApplicationsInt($value, 1, 2147483647);
    }
    return array_values(array_unique($values));
}
function ariaApplicationsCommand(string $state, array $profile, array $a): array {
    $operation = $a['operation'] ?? '';
    $type = $profile['type'];
    if (!in_array($operation, ariaApplicationsOperations($type)['write'], true)) ariaFail('application operation unavailable');
    $path = ''; $body = ''; $method = 'POST'; $extraHeaders = []; $ids = []; $hashes = [];
    if ($type === 'sonarr' || $type === 'radarr') {
        if (isset($a['hashes'])) ariaFail('invalid request');
        if ($operation !== 'rss_sync') $ids = ariaApplicationsIDs($a['ids'] ?? null);
        elseif (isset($a['ids'])) ariaFail('invalid request');
        if (in_array($operation, ['refresh', 'rescan'], true) && count($ids) !== 1) ariaFail('invalid request');
        if ($type === 'sonarr' && $operation === 'search' && count($ids) !== 1) ariaFail('invalid request');
        $names = $type === 'sonarr' ? ['refresh' => 'RefreshSeries', 'rescan' => 'RescanSeries', 'search' => 'SeriesSearch', 'rss_sync' => 'RssSync'] : ['refresh' => 'RefreshMovie', 'rescan' => 'RescanMovie', 'search' => 'MoviesSearch', 'rss_sync' => 'RssSync'];
        $command = ['name' => $names[$operation]];
        if ($ids) $command[$type === 'sonarr' ? 'seriesId' : ($operation === 'search' ? 'movieIds' : 'movieId')] = $type === 'radarr' && $operation === 'search' ? $ids : $ids[0];
        $path = '/api/'.$profile['api_version'].'/command'; $body = ariaJson($command); $extraHeaders = ['Content-Type: application/json'];
    } elseif ($type === 'plex') {
        if (isset($a['hashes'])) ariaFail('invalid request');
        $ids = ariaApplicationsIDs($a['ids'] ?? null);
        if (count($ids) !== 1) ariaFail('invalid request');
        // Plex's documented scan endpoint uses GET, but remains a queued mutation.
        $path = '/library/sections/'.$ids[0].'/refresh'; $method = 'GET';
    } else {
        if (isset($a['ids'])) ariaFail('invalid request');
        $hashes = ariaApplicationsIDs($a['hashes'] ?? null, true);
        $body = http_build_query(['hashes' => implode('|', $hashes)], '', '&', PHP_QUERY_RFC3986);
        $extraHeaders = ['Content-Type: application/x-www-form-urlencoded'];
    }
    $auth = ariaApplicationsCredential($state, $profile);
    $headers = ariaApplicationsAuth($profile, $auth);
    try {
        if ($type === 'qbittorrent') {
            $version = trim(ariaApplicationsHTTP($profile, 'GET', '/api/v2/app/version', $headers)['body']);
            if (!preg_match('/^v?(\d+)\./', $version, $match) || (int)$match[1] < 4) ariaFail('application operation unavailable');
            $command = ['pause' => (int)$match[1] >= 5 ? 'stop' : 'pause', 'resume' => (int)$match[1] >= 5 ? 'start' : 'resume', 'recheck' => 'recheck', 'reannounce' => 'reannounce'][$operation];
            $path = '/api/v2/torrents/'.$command;
        }
        // Once issued, a lost response cannot prove that the remote action failed.
        // The worker retains the request receipt and marks this outcome unknown.
        try {
            $reply = ariaApplicationsHTTP($profile, $method, $path, array_merge($headers, $extraHeaders), $body);
            $result = ['profile' => $profile['profile'], 'operation' => $operation, 'accepted' => true, 'application_completion_verified' => false, 'ids' => $ids, 'hashes' => $hashes];
            if ($type === 'sonarr' || $type === 'radarr') {
                $value = ariaApplicationsJSON($reply);
                if (!is_int($value['id'] ?? null) || $value['id'] < 1) ariaFail('application invalid response');
                $result['command'] = ariaApplicationsProject($value, ['id', 'name', 'status', 'queued', 'started', 'ended'], array_values($auth));
            }
        } catch (Throwable $e) {
            if (in_array($e->getMessage(), ['operation timed out', 'application connection failed', 'application response too large', 'application invalid response', 'application request failed'], true)) ariaFail('application outcome unknown');
            throw $e;
        }
        return $result;
    } finally {
        if ($type === 'qbittorrent') { try { ariaApplicationsHTTP($profile, 'POST', '/api/v2/auth/logout', $headers); } catch (Throwable $e) { } }
    }
}
function ariaApplicationsRead(string $state, string $action, array $a): array {
    $oldDeadline = $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] ?? null;
    $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] = microtime(true) + 7;
    try {
        if ($action === 'applications_profiles') {
            $rows = [];
            foreach (glob(ariaApplicationsDirectory($state).'/*.json') ?: [] as $file) {
                if (count($rows) >= 64) break;
                try { $profile = ariaApplicationsProfile($state, basename($file, '.json')); $rows[] = $profile + ['operations' => ariaApplicationsOperations($profile['type'])]; }
                catch (Throwable $e) { }
            }
            return ['profiles' => $rows, 'supported_types' => ARIA_APPLICATIONS_TYPES, 'credentials' => 'named host-only vault references; qBittorrent secret is JSON with username and password; others are raw API tokens', 'max_response_bytes' => ARIA_APPLICATIONS_MAX_RESPONSE];
        }
        if ($action === 'applications_profile_get') {
            $profile = ariaApplicationsProfile($state, $a['profile']);
            return $profile + ['operations' => ariaApplicationsOperations($profile['type'])];
        }
        if ($action === 'applications_query') {
            $profile = ariaApplicationsProfile($state, $a['profile']);
            return ['profile' => $profile['profile'], 'type' => $profile['type'], 'operation' => $a['operation'], 'checked_at' => ariaNow(), 'redacted' => true, 'data' => ariaApplicationsQuery($state, $profile, $a)];
        }
        if ($action === 'applications_activity') {
            $names = $a['profiles'] ?? null;
            if (!is_array($names) || count($names) < 1 || count($names) > 8) ariaFail('invalid request');
            $rows = [];
            foreach (array_unique($names) as $name) {
                ariaApplicationsName($name);
                try {
                    $profile = ariaApplicationsProfile($state, $name);
                    $operation = $profile['type'] === 'plex' ? 'sessions' : ($profile['type'] === 'qbittorrent' ? 'status' : 'queue');
                    $data = ariaApplicationsQuery($state, $profile, ['operation' => $operation, 'limit' => 20]);
                    $rows[] = ['profile' => $name, 'type' => $profile['type'], 'available' => true, 'operation' => $operation, 'data' => $data];
                } catch (Throwable $e) { $rows[] = ['profile' => $name, 'available' => false, 'error' => ariaError($e)]; }
            }
            return ['applications' => $rows, 'checked_at' => ariaNow(), 'redacted' => true, 'maintenance_safe' => null, 'note' => 'Activity is sampled and may be incomplete. Unavailable applications never imply idle.'];
        }
        if ($action === 'applications_download_trace') {
            $arr = ariaApplicationsProfile($state, $a['arr_profile']);
            $download = ariaApplicationsProfile($state, $a['download_profile']);
            if (!in_array($arr['type'], ['sonarr', 'radarr'], true) || $download['type'] !== 'qbittorrent') ariaFail('application operation unavailable');
            $hash = ariaApplicationsIDs([$a['download_id'] ?? null], true)[0];
            $matches = []; $complete = false; $errors = []; $torrent = null;
            try {
                // Search at most 600 pending imports. Large queues remain explicitly incomplete.
                for ($page = 1; $page <= 3; $page++) {
                    $data = ariaApplicationsQuery($state, $arr, ['operation' => 'queue', 'page' => $page, 'limit' => 200]);
                    foreach ($data['items'] as $row) if (is_string($row['downloadId'] ?? null) && strcasecmp($row['downloadId'], $hash) === 0) $matches[] = $row;
                    if (!empty($data['truncated'])) break;
                    if (isset($data['total_records']) && $page * 200 >= $data['total_records']) { $complete = true; break; }
                    if ($data['returned'] < 200) { $complete = true; break; }
                }
            } catch (Throwable $e) { $errors[] = ['profile' => $arr['profile'], 'error' => ariaError($e)]; }
            try { $torrent = ariaApplicationsQuery($state, $download, ['operation' => 'downloads', 'torrent_hash' => $hash, 'limit' => 1]); }
            catch (Throwable $e) { $errors[] = ['profile' => $download['profile'], 'error' => ariaError($e)]; }
            return ['download_id' => $hash, 'arr_profile' => $arr['profile'], 'download_profile' => $download['profile'], 'queue_matches' => $matches, 'queue_scan_complete' => $complete, 'download' => $torrent, 'errors' => $errors, 'checked_at' => ariaNow(), 'redacted' => true, 'note' => 'Compare queue statusMessages with torrent state and save_path/content_path. No match in an incomplete queue scan is inconclusive.'];
        }
        ariaFail('unsupported action');
    } finally {
        if ($oldDeadline === null) unset($GLOBALS['ARIA_APPLICATIONS_DEADLINE']); else $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] = $oldDeadline;
    }
}
function ariaApplicationsExecute(string $state, array $job): array {
    $a = $job['arguments']; $action = $job['action'];
    if ($action === 'applications_profile_save' || $action === 'applications_profile_delete') {
        $name = ariaApplicationsName($a['profile'] ?? null);
        $path = ariaApplicationsPath($state, $name, false);
        $expected = $a['expected_sha256'] ?? null;
        if (!is_string($expected) || !preg_match('/^(?:[a-f0-9]{64})?$/D', $expected)) ariaFail('invalid request');
        $lock = ariaLock($state, 'applications');
        try {
            $old = is_file($path) ? ariaReadFile($path, 16384) : null;
            if ($old === null && $expected !== '') ariaFail('hash mismatch');
            if ($old !== null && !hash_equals($expected, hash('sha256', $old))) ariaFail('hash mismatch');
            if ($action === 'applications_profile_delete' && $old === null) ariaFail('not found');
            $config = $action === 'applications_profile_save' ? ariaApplicationsConfig($a) : null;
            if ($config !== null) ariaApplicationsCredential($state, $config);
            if ($config !== null && $old === null && count(glob(ariaApplicationsDirectory($state).'/*.json') ?: []) >= 64) ariaFail('queue full');
            if ($old !== null) ariaAtomic($state.'/backups/'.ariaName($job['job_id'], 'id').'-application.json', $old);
            if ($old !== null) ariaHash($path, $expected);
            elseif (file_exists($path)) ariaFail('hash mismatch');
            if ($action === 'applications_profile_delete') {
                if (!unlink($path)) ariaFail('command failed');
                return ['profile' => $name, 'deleted' => true, 'backup_created' => true];
            }
            $bytes = ariaJson($config); ariaAtomic($path, $bytes);
            return ['profile' => $name, 'sha256' => hash('sha256', $bytes), 'backup_created' => $old !== null, 'connection_tested' => false];
        } finally { ariaUnlock($lock); }
    }
    if ($action === 'applications_command') {
        $profile = ariaApplicationsProfile($state, $a['profile'], $a['expected_sha256']);
        $oldDeadline = $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] ?? null;
        $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] = microtime(true) + 20;
        try { return ariaApplicationsCommand($state, $profile, $a); }
        finally { if ($oldDeadline === null) unset($GLOBALS['ARIA_APPLICATIONS_DEADLINE']); else $GLOBALS['ARIA_APPLICATIONS_DEADLINE'] = $oldDeadline; }
    }
    ariaFail('unsupported action');
}

/** Fail-closed, sampled maintenance gate. No application means no idle evidence. */
function ariaApplicationsActivity(string $state, array $profiles = []): array {
    if (!$profiles) {
        foreach (glob(ariaApplicationsDirectory($state).'/*.json') ?: [] as $file) $profiles[] = basename($file, '.json');
    }
    if (!$profiles || count($profiles) > 8) return ['idle' => false, 'active_count' => 0, 'unknown' => true, 'profiles' => [], 'checked_at' => ariaNow()];
    $reply = ariaApplicationsRead($state, 'applications_activity', ['profiles' => $profiles]);
    $rows = []; $unknown = false; $active = 0;
    foreach ($reply['applications'] as $row) {
        $count = null;
        if (!empty($row['available']) && empty($row['data']['truncated'])) {
            $data = $row['data'];
            if ($row['type'] === 'qbittorrent') {
                if (is_numeric($data['dl_info_speed'] ?? null) && is_numeric($data['up_info_speed'] ?? null)) $count = ($data['dl_info_speed'] > 0 || $data['up_info_speed'] > 0) ? 1 : 0;
            } elseif (isset($data['total_records']) && is_int($data['total_records'])) $count = max(0, $data['total_records']);
        }
        if ($count === null) $unknown = true; else $active += $count;
        $rows[] = ['profile' => $row['profile'], 'available' => $count !== null, 'active_count' => $count];
    }
    return ['idle' => !$unknown && $active === 0, 'active_count' => $active, 'unknown' => $unknown, 'profiles' => $rows, 'checked_at' => $reply['checked_at']];
}
