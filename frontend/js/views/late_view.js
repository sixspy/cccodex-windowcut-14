export const lateViewTemplate = `
<div class="card wide" v-show="tab===2">
  <h2>迟到桶</h2>
  <p class="muted">关窗后到达进桶。核销走甲路径（状态机冻结）；核销后整笔拒补，只有可补入态才能补入正式窗。</p>
  <p>合计 {{lateSummary.count||0}} 笔 / 金额 {{lateSummary.amount||0}} · 可补入 {{promotableIds.length}} 笔</p>
  <table>
    <thead><tr><th>窗口</th><th>键</th><th>时间</th><th>金额</th><th>原因</th><th>状态</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="e in lateItems" :key="'l'+e.id">
        <td>{{e.window_name}}</td>
        <td>{{e.biz_key}}</td>
        <td>{{e.event_ts}}</td>
        <td>{{e.amount}}</td>
        <td>{{e.reason}}</td>
        <td>{{e.status}}</td>
        <td>
          <button class="ghost" @click="promoteLate(e.id)">补入</button>
          <button class="ghost" @click="writeoffLate(e.id)">核销</button>
          <button class="ghost" @click="discardLate(e.id)">作废空桩</button>
        </td>
      </tr>
    </tbody>
  </table>
</div>
`;
