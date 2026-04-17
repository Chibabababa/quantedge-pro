<?php
/**
 * API Reverse Proxy
 * Usage: /proxy.php/api/recommend  →  localhost:5000/api/recommend
 * PATH_INFO carries the endpoint path after proxy.php
 */

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit();
}

// PATH_INFO = /api/recommend  (everything after proxy.php in the URL)
$path = '';
if (!empty($_SERVER['PATH_INFO'])) {
    $path = $_SERVER['PATH_INFO'];
} elseif (!empty($_SERVER['REDIRECT_URL'])) {
    $path = preg_replace('#^/proxy\.php#', '', $_SERVER['REDIRECT_URL']);
}

if (empty($path)) {
    $path = '/api/health';
}

// Build target URL
$qs  = $_SERVER['QUERY_STRING'] ?? '';
$url = 'http://127.0.0.1:5000' . $path . ($qs !== '' ? '?' . $qs : '');

// cURL request
$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_TIMEOUT, 90);
curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);

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
