<?php
/** Fixed, repository-owned extension registry shared with the MCP frontend. */
declare(strict_types=1);

function ariaExtensionSpecs(): array {
    static $specs = null;
    if ($specs !== null) return $specs;
    $specs = [];
    foreach (['operations', 'diagnostics', 'applications', 'automation', 'deployment'] as $module) {
        $manifest = __DIR__."/$module-tools.json";
        if (!is_file($manifest)) continue;
        $rows = json_decode((string)file_get_contents($manifest), true);
        if (!is_array($rows)) ariaFail('internal error');
        require_once __DIR__."/host-$module.php";
        foreach ($rows as $row) {
            if (!is_array($row) || !preg_match('/^'.preg_quote($module, '/').'_[a-z0-9_]+$/D', $row['action'] ?? '') || isset($specs[$row['action']])) ariaFail('internal error');
            $row['module'] = $module;
            $specs[$row['action']] = $row;
        }
    }
    return $specs;
}

function ariaSchemaValue($value, array $schema, int $depth = 0) {
    if ($depth > 16) ariaFail('invalid request');
    $type = $schema['type'] ?? '';
    if (isset($schema['enum']) && !in_array($value, $schema['enum'], true)) ariaFail('invalid request');
    if ($type === 'string') {
        if (!is_string($value) || strpos($value, "\0") !== false || !preg_match('//u', $value)) ariaFail('invalid request');
        $length = preg_match_all('/./us', $value);
        if ($length < ($schema['minLength'] ?? 0) || $length > ($schema['maxLength'] ?? 131072) || strlen($value) > ($schema['maxBytes'] ?? ARIA_MAX_XML)) ariaFail('invalid request');
        if (isset($schema['pattern']) && preg_match('~'.str_replace('~', '\\~', $schema['pattern']).'~uD', $value) !== 1) ariaFail('invalid request');
    } elseif ($type === 'integer' || $type === 'number') {
        if (($type === 'integer' && !is_int($value)) || ($type === 'number' && !is_int($value) && !is_float($value)) || !is_finite((float)$value) || $value < ($schema['minimum'] ?? -PHP_INT_MAX) || $value > ($schema['maximum'] ?? PHP_INT_MAX)) ariaFail('invalid request');
    } elseif ($type === 'boolean') {
        if (!is_bool($value)) ariaFail('invalid request');
    } elseif ($type === 'array') {
        if (!is_array($value) || ($value !== [] && array_keys($value) !== range(0, count($value)-1)) || count($value) < ($schema['minItems'] ?? 0) || count($value) > ($schema['maxItems'] ?? 128)) ariaFail('invalid request');
        foreach ($value as $key => $item) $value[$key] = ariaSchemaValue($item, $schema['items'], $depth + 1);
        if (!empty($schema['uniqueItems']) && count(array_unique(array_map('ariaJson', $value))) !== count($value)) ariaFail('invalid request');
    } elseif ($type === 'object') {
        if (!is_array($value) || ($value !== [] && array_keys($value) === range(0, count($value)-1)) || count($value) > ($schema['maxProperties'] ?? 128)) ariaFail('invalid request');
        $properties = $schema['properties'] ?? [];
        if (array_diff($schema['required'] ?? [], array_keys($value))) ariaFail('invalid request');
        foreach ($value as $key => $item) {
            if (!is_string($key) || strlen($key) > 256 || strpos($key, "\0") !== false) ariaFail('invalid request');
            if (isset($properties[$key])) $value[$key] = ariaSchemaValue($item, $properties[$key], $depth + 1);
            elseif (is_array($schema['additionalProperties'] ?? false)) $value[$key] = ariaSchemaValue($item, $schema['additionalProperties'], $depth + 1);
            elseif (($schema['additionalProperties'] ?? false) !== true) ariaFail('invalid request');
        }
        foreach ($properties as $key => $field) if (!array_key_exists($key, $value) && array_key_exists('default', $field)) $value[$key] = ariaSchemaValue($field['default'], $field, $depth + 1);
    } else ariaFail('internal error');
    return $value;
}

function ariaExtensionValidate(string $action, array $arguments): ?array {
    $spec = ariaExtensionSpecs()[$action] ?? null;
    if ($spec === null) return null;
    $value = ariaSchemaValue($arguments, ['type' => 'object', 'properties' => $spec['properties'], 'required' => $spec['required'] ?? [], 'additionalProperties' => false]);
    if (empty($spec['read_only'])) ariaName($value['request_id'] ?? null, 'id');
    return $value;
}

function ariaExtensionRead(string $state, string $action, array $arguments): array {
    $spec = ariaExtensionSpecs()[$action] ?? null;
    if ($spec === null || empty($spec['read_only'])) ariaFail('unsupported action');
    $handler = 'aria'.ucfirst($spec['module']).'Read';
    return $handler($state, $action, $arguments);
}

function ariaExtensionExecute(string $state, array $job): array {
    $spec = ariaExtensionSpecs()[$job['action']] ?? null;
    if ($spec === null || !empty($spec['read_only'])) ariaFail('unsupported action');
    $handler = 'aria'.ucfirst($spec['module']).'Execute';
    return $handler($state, $job);
}

function ariaExtensionAuthorize(string $state, string $action, array $arguments): void {
    ariaExtensionSpecs();
    if (function_exists('ariaDeploymentAuthorize')) ariaDeploymentAuthorize($state, $action, $arguments);
}
