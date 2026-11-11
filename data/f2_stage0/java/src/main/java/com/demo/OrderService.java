package com.demo;

/** 订单服务（白包阶段2 Java 验收 demo）。
 *
 * 埋缺陷：getTier 的 A 档边界用了 >（应为 >=）——score==90 应得 "A" 实际得 "B"，
 * 边界值用例（score=90）是缺陷证人。
 */
public class OrderService {

    public double applyDiscount(double amount, int percent) {
        if (percent < 0 || percent > 100) {
            throw new IllegalArgumentException("percent out of range");
        }
        return amount * (100 - percent) / 100.0;
    }

    public String getTier(int score) {
        if (score > 90) {          // 缺陷：应为 >= 90
            return "A";
        }
        if (score >= 60) {
            return "B";
        }
        return "C";
    }

    public boolean isValidCode(String code) {
        return code != null && code.matches("[A-Za-z0-9]{2,8}");
    }
}
