<?php
use PHPUnit\Framework\TestCase;

require __DIR__ . '/../src/Shop.php';

final class ShopTest extends TestCase
{
    public function testTierBoundary(): void
    {
        $this->assertSame('A', Shop::getTier(90));
    }

    public function testAdd(): void
    {
        $this->assertSame(3, Shop::add(1, 2));
    }
}
