<?php

use App\Http\Controllers\OrderController;
use Illuminate\Support\Facades\Route;

Route::post('/api/orders/{id}/cancel', [OrderController::class, 'cancel']);
