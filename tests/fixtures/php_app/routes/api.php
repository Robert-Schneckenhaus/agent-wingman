<?php

use App\Http\Controllers\OrderController;
use App\Http\Controllers\ShowOrderController;
use Illuminate\Support\Facades\Route;

Route::post('/api/orders/{id}/cancel', [OrderController::class, 'cancel']);
Route::get('/api/orders/{id}', ShowOrderController::class);
