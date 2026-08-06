/**
 * 404(A-36)。
 *
 * 在它之前,兜底路由是 `<Navigate to="/today" replace />` —— 任何一个坏链接、
 * 任何一次部署 base-path 配错、任何一条改过名的路由,表现都是「莫名其妙回到
 * 了今日」。而那个表现**看起来完全正常**,于是没有人会报:运营以为自己点错了。
 *
 * base-path 配错尤其隐蔽:整站每一个深链都跳首页,而首页本身是好的,
 * 冒烟测试一路绿。
 *
 * 所以这一页要做两件事:说清楚是路径不存在(不是权限、不是后端挂了),
 * 并且**把那个路径打出来** —— 排查这类问题第一句话就是「你访问的是哪个地址」。
 */
import { Button, Result, Space, Typography } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function NotFoundPage() {
  useDocumentTitle('页面不存在')
  const navigate = useNavigate()
  const location = useLocation()

  return (
    <Result
      status="404"
      title="这个地址没有对应的页面"
      subTitle={
        <Space direction="vertical" size={4}>
          <Typography.Text type="secondary">
            不是权限问题,也不是后端故障 —— 前端路由表里没有这条路径。
          </Typography.Text>
          <Typography.Text code copyable>
            {location.pathname}
            {location.search}
          </Typography.Text>
          <Typography.Text type="secondary">
            如果站内每个链接都落到这一页,多半是部署的 base-path 配错了;
            报障时请把上面这个地址一起带上。
          </Typography.Text>
        </Space>
      }
      extra={
        <Space>
          <Button type="primary" onClick={() => navigate('/today')}>
            回到今日
          </Button>
          <Button onClick={() => navigate(-1)}>返回上一页</Button>
        </Space>
      }
    />
  )
}
