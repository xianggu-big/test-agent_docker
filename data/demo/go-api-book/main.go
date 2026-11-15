// 最小 Go REST 应用（F 卡阶段 1 #7 真机验收载体）。
// 埋缺陷：DELETE /books/{id} 未做幂等保护——同 id 重复删除第二次返回 404
// （需求：删除应幂等，重复删除同一 id 仍返回 200）。
package main

import (
	"net/http"
	"os"
	"strconv"

	"github.com/gin-gonic/gin"
)

type book struct {
	ID    int    `json:"id"`
	Title string `json:"title"`
}

var books = map[int]book{}
var nextID = 1

func setupRouter() *gin.Engine {
	r := gin.New()
	r.GET("/ping", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"message": "pong"})
	})
	r.GET("/books", func(c *gin.Context) {
		list := make([]book, 0, len(books))
		for _, b := range books {
			list = append(list, b)
		}
		c.JSON(http.StatusOK, list)
	})
	r.POST("/books", func(c *gin.Context) {
		var b book
		if err := c.ShouldBindJSON(&b); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		b.ID = nextID
		nextID++
		books[b.ID] = b
		c.JSON(http.StatusCreated, b)
	})
	r.DELETE("/books/:id", func(c *gin.Context) {
		// 缺陷埋点：不检查是否存在，第一次删非存在 id 返回 200（需求：未找到返回 404），
		// 且 map 删除对不存在键是 no-op——重复删除第二次 id 与首次行为一致，
		// 但"删除未创建的 id"返回 200 而非 404 = 契约违背（REST-05 同型）。
		idStr := c.Param("id")
		id, err := strconv.Atoi(idStr)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid id"})
			return
		}
		delete(books, id)
		c.JSON(http.StatusOK, gin.H{"deleted": id})
	})
	return r
}

func main() {
	// 端口契约（2026-11-15 修）：provision 以 --port 下发（L2/L3 探针与契约 BASE
	// 都按该端口）——旧版硬编码 8080 导致服务起了但探针在 8000 上空等
	port := 8080
	for i, arg := range os.Args {
		if arg == "--port" && i+1 < len(os.Args) {
			if p, err := strconv.Atoi(os.Args[i+1]); err == nil {
				port = p
			}
		}
	}
	r := setupRouter()
	_ = r.Run("127.0.0.1:" + strconv.Itoa(port))
}
