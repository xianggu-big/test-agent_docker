# mta-test:php83 —— PHP 执行镜像（多语言工具链接入阶段4，白包）
# php:8.3-cli 官方基座 + composer（composer:2 官方镜像拷贝）+ pcov（pecl 编译，覆盖率驱动）
# + zip 扩展与 unzip（composer dist 下载必需——真机实锤：缺 zip 时 composer install
#   "The zip extension and unzip/7z commands are both missing" 直接失败）。
# PHPUnit 等测试依赖随项目走（composer.json → composer install，依赖装进容器工作区）。
FROM php:8.3-cli
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make autoconf libc-dev pkg-config \
       libzip-dev unzip \
    && docker-php-ext-install zip \
    && pecl install pcov \
    && docker-php-ext-enable pcov \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
