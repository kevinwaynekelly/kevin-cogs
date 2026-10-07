<?php
/** Optional loopback-only administration UI. The existing agent owns all effects. */
declare(strict_types=1);
require_once __DIR__.'/host-agent.php';

function ariaDashboardState(string $state): string {
    if ($state === '' || $state[0] !== '/' || preg_match('/[\x00-\x1f\x7f]/', $state)) ariaFail('invalid request');
    $state = rtrim($state, '/');
    $parts = explode('/', substr($state, 1)); $prefix = '';
    if ($state === '' || array_intersect($parts, ['', '.', '..'])) ariaFail('invalid request');
    foreach ($parts as $part) { $prefix .= '/'.$part; if (is_link($prefix)) ariaFail('invalid request'); }
    ariaInit($state);
    return $state;
}
function ariaDashboardToken(string $state, bool $create = false): string {
    $lock = ariaLock($state, 'dashboard-init');
    try {
        $path = "$state/dashboard-token";
        if (is_link($path)) ariaFail('invalid request');
        if (!file_exists($path) && $create) ariaAtomic($path, bin2hex(random_bytes(32))."\n", 0600);
        if (!is_file($path) || (!ariaTest() && fileowner($path) !== 0)) ariaFail('not found');
        if (!chmod($path, 0600)) ariaFail('internal error');
        $token = trim(ariaReadFile($path, 65));
        if (!preg_match('/^[a-f0-9]{64}$/D', $token)) ariaFail('internal error');
        return $token;
    } finally { ariaUnlock($lock); }
}
function ariaDashboardCatalog(): array {
    $string = ['type' => 'string'];
    $hash = ['type' => 'string', 'pattern' => '^[a-f0-9]{64}$'];
    $request = ['type' => 'string', 'pattern' => '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'];
    $core = [
        ['capabilities', 'Management capabilities', [], [], true],
        ['containers', 'List containers', [], [], true],
        ['container_inspect', 'Inspect container', ['name' => $string], ['name'], true],
        ['container_logs', 'Read container log tail', ['name' => $string, 'tail' => ['type' => 'integer', 'default' => 100]], ['name'], true],
        ['container_action', 'Start, stop or restart container', ['name' => $string, 'action' => ['type' => 'string', 'enum' => ['start', 'stop', 'restart']]], ['name', 'action'], false],
        ['templates_list', 'List saved container templates', [], [], true],
        ['template_get', 'Read a redacted template and its hash', ['template' => $string], ['template'], true],
        ['template_save', 'Save template with expected hash and backup', ['template' => $string, 'xml' => $string, 'expected_sha256' => $string], ['template', 'xml', 'expected_sha256'], false],
        ['template_deploy', 'Deploy saved template', ['template' => $string, 'expected_sha256' => $hash, 'pull' => ['type' => 'boolean', 'default' => true], 'start' => ['type' => 'boolean', 'default' => true]], ['template', 'expected_sha256'], false],
        ['scripts_list', 'List installed User Scripts', [], [], true],
        ['script_get', 'Read installed User Script', ['name' => $string], ['name'], true],
        ['script_run', 'Run reviewed User Script as root', ['name' => $string, 'expected_sha256' => $hash, 'timeout_seconds' => ['type' => 'integer', 'default' => 3600]], ['name', 'expected_sha256'], false],
        ['container_update', 'Update one container', ['name' => $string], ['name'], false],
        ['containers_update_all', 'Update supported containers', [], [], false],
        ['job_status', 'Read queued job status', ['job_id' => $string], ['job_id'], true],
        ['bridge_update', 'Update bridge from its fixed repository', [], [], false],
        ['bridge_update_status', 'Read bridge update status', [], [], true],
    ];
    $rows = [];
    foreach ($core as [$action, $description, $properties, $required, $read]) {
        if (!$read) { $properties['request_id'] = $request; $required[] = 'request_id'; }
        $rows[] = ['action' => $action, 'description' => $description, 'properties' => (object)$properties, 'required' => $required, 'read_only' => $read];
    }
    foreach (ariaExtensionSpecs() as $row) {
        unset($row['module']); $row['properties'] = (object)$row['properties']; $rows[] = $row;
    }
    return ['actions' => $rows, 'execution' => 'existing local agent; mutations remain durable queued jobs'];
}
function ariaDashboardRequest(string $body): array {
    if (strlen($body) > ARIA_MAX_INPUT) ariaFail('invalid request');
    $object = json_decode($body, false, 32);
    if (!($object instanceof stdClass) || !isset($object->arguments) || !($object->arguments instanceof stdClass)) ariaFail('invalid request');
    $data = json_decode($body, true, 32);
    if (!is_array($data) || array_diff(array_keys($data), ['action', 'arguments']) || !is_string($data['action'] ?? null)) ariaFail('invalid request');
    $arguments = ariaValidate($data['action'], $data['arguments']);
    return ['action' => $data['action'], 'arguments' => $arguments];
}
function ariaDashboardForward(array $request): array {
    $socketPath = ariaPath('ARIA_DASHBOARD_SOCKET', '/var/run/aria-gpt-bridge/agent.sock');
    if ($socketPath === '' || $socketPath[0] !== '/' || is_link($socketPath)) ariaFail('invalid request');
    $socket = @stream_socket_client('unix://'.$socketPath, $errno, $error, 2);
    if ($socket === false) ariaFail('management busy');
    try {
        stream_set_timeout($socket, 10);
        $wire = ariaJson($request)."\n";
        if (strlen($wire) > ARIA_MAX_INPUT) ariaFail('invalid request');
        $offset = 0;
        while ($offset < strlen($wire)) {
            $written = @fwrite($socket, substr($wire, $offset));
            if ($written === false || $written === 0) ariaFail('operation outcome unknown');
            $offset += $written;
        }
        $reply = @fgets($socket, ARIA_MAX_INPUT + 2);
        if ($reply === false || strlen($reply) > ARIA_MAX_INPUT || substr($reply, -1) !== "\n") ariaFail('operation outcome unknown');
        $data = json_decode($reply, true);
        if (!is_array($data) || !is_bool($data['ok'] ?? null)) ariaFail('operation outcome unknown');
        if (!$data['ok']) return ['ok' => false, 'error' => in_array($data['error'] ?? '', ARIA_ERRORS, true) ? $data['error'] : 'internal error'];
        if (!is_array($data['result'] ?? null)) ariaFail('operation outcome unknown');
        return ['ok' => true, 'result' => $data['result']];
    } finally { fclose($socket); }
}
function ariaDashboardReply(int $status, array $data): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    $body = ariaJson($data);
    if (strlen($body) > ARIA_MAX_INPUT) { http_response_code(502); $body = ariaJson(['ok' => false, 'error' => 'response exceeds limit']); }
    echo $body;
}
function ariaDashboardPage(): string {
    $nonce = bin2hex(random_bytes(16));
    header("Content-Security-Policy: default-src 'none'; script-src 'nonce-$nonce'; style-src 'nonce-$nonce'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'");
    return str_replace('__NONCE__', $nonce, <<<'HTML'
<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aria local management</title>
<style nonce="__NONCE__">:root{color-scheme:dark}body{font:16px system-ui,sans-serif;background:#121b28;color:#edf3fa;max-width:1100px;margin:2rem auto;padding:0 1rem}h1{margin-bottom:.3rem}p{line-height:1.5}label{display:block;margin-top:1rem;font-weight:600}input,select,textarea,button{font:inherit;padding:.65rem;border:1px solid #64748b;border-radius:6px;background:#1e293b;color:inherit}input,select,textarea{box-sizing:border-box;width:100%}textarea,pre{font:14px ui-monospace,monospace;line-height:1.5}textarea{min-height:230px;resize:vertical}button{cursor:pointer;margin:.7rem .4rem .3rem 0}button:disabled{opacity:.5;cursor:default}.primary{background:#2456b8;border-color:#6394ef}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0c1420;padding:1rem;border-radius:6px;max-height:36rem;overflow:auto}.muted{color:#b8c5d9}.status{min-height:1.5rem}details{margin:1rem 0}[hidden]{display:none!important}</style>
<h1>Aria local management</h1><p class="muted">Run server tasks through the local management agent, even when ChatGPT is disconnected.</p>
<section id="login"><label for="token">Local dashboard token</label><input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="Read management/dashboard-token in Aria’s root terminal"><button id="connect" class="primary">Connect</button><p class="muted">The token stays in this page’s memory. Reloading or disconnecting clears it. This interface runs on localhost.</p></section>
<section id="workspace" hidden><button id="disconnect">Disconnect</button><label for="action">Operation</label><select id="action"></select><p id="description"></p><p id="mode" class="muted"></p><label for="arguments">Arguments as JSON</label><textarea id="arguments" spellcheck="false"></textarea><button id="run" class="primary">Run operation</button><button id="newid">New request ID</button><button id="refresh" disabled>Refresh job status</button><details><summary>Argument schema</summary><pre id="schema"></pre></details></section>
<p id="status" class="status" role="status" aria-live="polite"></p><pre id="result" aria-label="Operation result">Connect to inspect and manage Aria.</pre>
<script nonce="__NONCE__">
(()=>{'use strict';const el=id=>document.getElementById(id);let token='',catalog=[],generation=0,lastJob=null;
const show=value=>{el('result').textContent=JSON.stringify(value,null,2)};
const requestId=()=>{const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return 'local-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')};
async function api(path,body){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);try{const options={method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token},signal:controller.signal,cache:'no-store',credentials:'omit'};if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}const response=await fetch(path,options),data=await response.json();if(!response.ok||data.ok===false)throw new Error(data.error||'Request failed');return data.result}finally{clearTimeout(timer)}}
function sample(field){if(Object.prototype.hasOwnProperty.call(field,'default'))return field.default;if(field.enum)return field.enum[0];if(field.type==='object'){const o={};for(const key of field.required||[])o[key]=sample(field.properties[key]);return o}if(field.type==='array')return [];if(field.type==='boolean')return false;if(field.type==='integer'||field.type==='number')return field.minimum||0;return ''}
function select(){const spec=catalog[Number(el('action').value)],args={};for(const key of spec.required||[])args[key]=key==='request_id'?requestId():sample(spec.properties[key]);el('arguments').value=JSON.stringify(args,null,2);el('description').textContent=spec.description;el('mode').textContent=spec.read_only?'Reads server information.':'Can change the server. Review arguments before running. Reuse the request ID after an uncertain result.';el('schema').textContent=JSON.stringify({type:'object',properties:spec.properties,required:spec.required,additionalProperties:false},null,2);el('newid').disabled=spec.read_only}
async function poll(job,current){if(current!==generation)return;try{const result=await api('/api/dispatch',{action:job.bridge?'bridge_update_status':'job_status',arguments:job.bridge?{}:{job_id:job.id}});if(current!==generation)return;show(result);el('status').textContent='Job '+(result.status||'status received');if(['queued','running'].includes(result.status))setTimeout(()=>poll(job,current),2000)}catch(error){if(current===generation)el('status').textContent='Could not refresh this job. Keep its request ID and refresh after the agent reconnects.'}}
el('connect').addEventListener('click',async()=>{token=el('token').value.trim();el('token').value='';el('connect').disabled=true;try{catalog=(await api('/api/catalog')).actions;el('action').replaceChildren();catalog.forEach((spec,index)=>{const option=document.createElement('option');option.value=String(index);option.textContent=spec.action.replaceAll('_',' ')+' · '+(spec.read_only?'read':'change');el('action').append(option)});el('login').hidden=true;el('workspace').hidden=false;select();el('status').textContent='Connected locally.';show({connected:true,operations:catalog.length})}catch(error){token='';el('status').textContent=error.message}finally{el('connect').disabled=false}});
el('disconnect').addEventListener('click',()=>{token='';catalog=[];generation++;lastJob=null;el('workspace').hidden=true;el('login').hidden=false;el('arguments').value='';el('action').replaceChildren();el('schema').textContent='';el('result').textContent='Disconnected.';el('status').textContent='';el('refresh').disabled=true});
el('action').addEventListener('change',select);
el('newid').addEventListener('click',()=>{try{const args=JSON.parse(el('arguments').value);if(!args||typeof args!=='object'||Array.isArray(args))throw new Error('Arguments must be an object');args.request_id=requestId();el('arguments').value=JSON.stringify(args,null,2)}catch(error){el('status').textContent=error.message}});
el('run').addEventListener('click',async()=>{const current=++generation;el('run').disabled=true;lastJob=null;el('refresh').disabled=true;try{const spec=catalog[Number(el('action').value)],args=JSON.parse(el('arguments').value);if(!args||typeof args!=='object'||Array.isArray(args))throw new Error('Arguments must be a JSON object');if(!spec.read_only&&!args.request_id){args.request_id=requestId();el('arguments').value=JSON.stringify(args,null,2)}el('status').textContent='Submitting operation…';const result=await api('/api/dispatch',{action:spec.action,arguments:args});if(current!==generation)return;show(result);el('status').textContent=result.status?'Operation '+result.status:'Complete.';if(result.job_id){lastJob={id:result.job_id,bridge:spec.action==='bridge_update'};el('refresh').disabled=false;if(['queued','running'].includes(result.status))setTimeout(()=>poll(lastJob,current),1000)}}catch(error){if(current===generation)el('status').textContent=error.message+'. If submission timed out, reuse this request ID to check the same operation.'}finally{el('run').disabled=false}});
el('refresh').addEventListener('click',()=>{if(lastJob)poll(lastJob,++generation)});
})();
</script></html>
HTML
    );
}
function ariaDashboardHttp(): void {
    header('Cache-Control: no-store, max-age=0'); header('Pragma: no-cache');
    header('X-Content-Type-Options: nosniff'); header('X-Frame-Options: DENY');
    header('Referrer-Policy: no-referrer'); header('Permissions-Policy: camera=(), microphone=(), geolocation=()');
    $port = ariaTest() ? (string)(getenv('ARIA_DASHBOARD_PORT') ?: '8786') : '8786';
    $host = $_SERVER['HTTP_HOST'] ?? '';
    if (!in_array($host, ['127.0.0.1:'.$port, 'localhost:'.$port], true) || !in_array($_SERVER['REMOTE_ADDR'] ?? '', ['127.0.0.1', '::1'], true)) { ariaDashboardReply(403, ['ok' => false, 'error' => 'loopback access required']); return; }
    if ((isset($_SERVER['HTTP_ORIGIN']) && $_SERVER['HTTP_ORIGIN'] !== 'http://'.$host) || (isset($_SERVER['HTTP_SEC_FETCH_SITE']) && !in_array($_SERVER['HTTP_SEC_FETCH_SITE'], ['same-origin', 'none'], true))) { ariaDashboardReply(403, ['ok' => false, 'error' => 'origin rejected']); return; }
    $uri = $_SERVER['REQUEST_URI'] ?? ''; $method = $_SERVER['REQUEST_METHOD'] ?? '';
    if ($uri === '/health' && $method === 'GET') { ariaDashboardReply(200, ['service' => 'aria-local-dashboard', 'pid' => getmypid()]); return; }
    if ($uri === '/' && $method === 'GET') { header('Content-Type: text/html; charset=utf-8'); echo ariaDashboardPage(); return; }
    if (!in_array($uri, ['/api/catalog', '/api/dispatch'], true)) { ariaDashboardReply(404, ['ok' => false, 'error' => 'not found']); return; }
    try {
        $state = ariaDashboardState((string)getenv('ARIA_DASHBOARD_STATE'));
        $authorization = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
        if (!preg_match('/^Bearer ([a-f0-9]{64})$/D', $authorization, $match) || !hash_equals(ariaDashboardToken($state), $match[1])) { ariaDashboardReply(401, ['ok' => false, 'error' => 'authentication required']); return; }
        if ($uri === '/api/catalog' && $method === 'GET') { ariaDashboardReply(200, ['ok' => true, 'result' => ariaDashboardCatalog()]); return; }
        if ($uri !== '/api/dispatch' || $method !== 'POST') { ariaDashboardReply(405, ['ok' => false, 'error' => 'method not allowed']); return; }
        if (strtolower(trim(explode(';', $_SERVER['CONTENT_TYPE'] ?? '')[0])) !== 'application/json') { ariaDashboardReply(415, ['ok' => false, 'error' => 'JSON body required']); return; }
        $length = $_SERVER['CONTENT_LENGTH'] ?? '';
        if (isset($_SERVER['HTTP_TRANSFER_ENCODING']) || !ctype_digit($length) || (float)$length > ARIA_MAX_INPUT) { ariaDashboardReply(413, ['ok' => false, 'error' => 'request exceeds limit']); return; }
        $stream = fopen('php://input', 'rb');
        $body = stream_get_contents($stream, ARIA_MAX_INPUT + 1); fclose($stream);
        if ($body === false || strlen($body) !== (int)$length || strlen($body) > ARIA_MAX_INPUT) ariaFail('invalid request');
        $reply = ariaDashboardForward(ariaDashboardRequest($body));
        ariaDashboardReply($reply['ok'] ? 200 : 422, $reply);
    } catch (Throwable $e) { ariaDashboardReply($e->getMessage() === 'operation outcome unknown' ? 504 : 422, ['ok' => false, 'error' => ariaError($e)]); }
}
if (PHP_SAPI === 'cli-server') ariaDashboardHttp();
elseif (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try {
        if (count($argv) !== 3 || $argv[1] !== 'init') ariaFail('invalid request');
        ariaDashboardToken(ariaDashboardState($argv[2]), true);
        echo "Local dashboard credentials initialized.\n";
    } catch (Throwable $e) { fwrite(STDERR, ariaError($e)."\n"); exit(1); }
}
