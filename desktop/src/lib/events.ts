/** 跨页数据变更广播（2026-10-26 第三轮验收）。
 *
 * 主人：「改完场景后需要同步到选择场景的地方，比如改了场景名字，
 * 群列表右键显示的就应该是改后的场景名字，这种低级错误不能有。」
 *
 * 为什么用 window 事件而不是 prop/context：
 * 「人格规则」和「群管理」是**兄弟页**（`{page === 'x' && <X />}` 条件渲染），
 * 它们之间没有父子关系，为了传一个版本号去搭 context 会把两页绑死。
 * 自定义事件只有一个约定（事件名），谁关心谁监听。
 *
 * 注意：这是**同一窗口内**的同步。跨窗口（如果以后开双窗）要换成 IPC，
 * 那时改这一个文件就够。
 */
export const DATA_CHANGED = 'tangtang:data-changed'

export function notifyDataChanged(): void {
  window.dispatchEvent(new Event(DATA_CHANGED))
}
