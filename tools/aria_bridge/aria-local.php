<?php
/** Offline root CLI: php aria-local.php STATE ACTION '{"arguments":"here"}'. */
declare(strict_types=1);
require_once __DIR__.'/host-dashboard.php';
try {
    if (count($argv) < 3 || count($argv) > 4) ariaFail('invalid request');
    $state = ariaDashboardState($argv[1]);
    if ($argv[2] === '--catalog' && count($argv) === 3) $result = ariaDashboardCatalog();
    else {
        $json = $argv[3] ?? '{}';
        if ($json === '-') $json = stream_get_contents(STDIN, ARIA_MAX_INPUT + 1);
        if (!is_string($json) || strlen($json) > ARIA_MAX_INPUT) ariaFail('invalid request');
        $object = json_decode($json);
        if (!($object instanceof stdClass)) ariaFail('invalid request');
        $request = ariaDashboardRequest(ariaJson(['action' => $argv[2], 'arguments' => $object]));
        $result = ariaDispatch($state, $request);
    }
    echo ariaJson(['ok' => true, 'result' => $result])."\n";
} catch (Throwable $e) { echo ariaJson(['ok' => false, 'error' => ariaError($e)])."\n"; exit(1); }
