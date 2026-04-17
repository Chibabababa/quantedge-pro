<?php
/**
 * API Reverse Proxy
 * Routes /api/* requests to Flask backend on localhost:5000
 * .htaccess rewrites /api/X to proxy.php?_path=X
 */

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit();
}

// Get endpoint path (set by .htaccess RewriteRule)
$api_path = $_GET['_path'] ?? 'health';

// Build query string (exclude our internal _path param)
$params = $_GET;
unset($params['_path']);
$qs = http_build_query($params);

// Build target URL
$target = 'http://127.0.0.1:5000/api/' . ltrim($api_path, '/');
if ($qs !== '') {
    $target .= '?' . $qs;
}

// Setup cURL
$ch = curl_init($target);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_TIMEOUT, 90);
curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);

// Forward POST body
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $body = file_get_contents('php://input');
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
}

$response  = curl_exec($ch);
$http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curl_err  = curl_error($ch);
curl_close($ch);

header('Content-Type: application/json');

if ($curl_err || $response === false) {
    http_response_code(503);
    echo json_encode(['error' => 'backend_unavailable', 'message' => $curl_err]);
    exit();
}

http_response_code($http_code);
echo $response;
