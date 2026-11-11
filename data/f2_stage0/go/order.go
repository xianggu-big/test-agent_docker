// Package shop：订单分级服务（白包阶段3 Go 真机验收载体）
// 埋缺陷：GetTier(90) 边界——需求是 >= 90 为 "A"，实现写成了 > 90，
// 90 端点落入 else 分支返回 "B"（与 java-shop getTier 同题，可跨语言对照）。
package shop

// GetTier 按积分分级：>=90 "A"，>=60 "B"，其余 "C"。
func GetTier(score int) string {
	if score > 90 {
		return "A"
	}
	if score >= 60 {
		return "B"
	}
	return "C"
}

// Add 简单加法（生成用例的对照函数，行为正确）。
func Add(a, b int) int {
	return a + b
}
