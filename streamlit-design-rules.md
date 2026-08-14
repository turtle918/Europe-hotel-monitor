Streamlit 界面设计规范

视觉排版：你生成界面时必须使用 st.container 划分功能区域。你必须使用 st.columns 控制元素的宽度。你不能在页面上直接堆叠原始表格。

交互反馈：你必须在执行数据库查询任务时调用 st.spinner 显示加载状态。你必须在数据更新完成后调用 st.toast 弹出提示。

数据展示：你必须使用 st.metric 展示核心的价格变动数据。你必须把非核心的详细数据放入 st.expander 折叠面板中。

样式覆写：你必须使用 st.markdown 注入 CSS 代码。你需要通过 CSS 隐藏 Streamlit 默认的顶部菜单栏和底部标志。