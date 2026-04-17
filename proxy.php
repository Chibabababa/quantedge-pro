<?php
/**
 * API Reverse Proxy
 * Routes /api/* requests to Flask backend on localhost:5000
 */

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit();
}

// Get the path from PATH_INFO or REDIRECT_URL
$path = '';
if (!empty($_SERVER['PATH_INFO'])) {
    $path = $_SERVER['PATH_INFO'];
} elseif (!empty($_SERVER['REDIRECT_URL'])) {
    // Strip leading /proxy.php if present
    $path = preg_replace('#^/proxy\.php#', '', $_SERVER['REDIRECT_URL']);
}

// Build target URL
$target = 'http://127.0.0.1:5000' . $path;

// Append query string if present
$qs = $_SERVER['QUERY_STRING'] ?? '';
if ($qs !== '') {
    $target .= '?' . $qs;
}

// Setup cURL
$ch = curl_init($target);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_TIMEOUT, 90);
curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
curl_setopt($ch, CURLOPT_HEADER, true);

// Forward POST body
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $body = file_get_contents('php://input');
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
}

$full_response = curl_exec($ch);
$http_code     = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$header_size   = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
$curl_error    = curl_error($ch);
curl_close($ch);

if ($curl_error || $full_response === false) {
    http_response_code(503);
    header('Content-Type: application/json');
    echo json_encode([
        'error'   => 'backend_unavailable',
        'message' => $curl_error ?: 'No response from Flask backend'
    ]);
    exit();
}

$body = substr($full_response, $header_size);

http_response_code($http_code);
header('Content-Type: application/json');
echo $body;
