"""核对示例里的 verified: 标签是否真的能在语料里按「集 + 时间码 + 原文」找到。

AUD-10 的原话是「标签格式能通过检查，但标签本身没有网页、章节或原文定位」。
光靠格式正则拦不住这种事——`verified:消失@0:25:56` 和 `verified:消失@9:99:99`
在正则眼里一模一样。这个脚本把「verified」这个词变成**可执行的承诺**：

    verified:<集>@<时间码>   ⇒   索引里那一集、那个时间码上的原文，必须与示例逐字一致

`handwritten:` / `reading:` 不查。它们本来就不声称有原句，查它们只会逼人编出处。

用法：
    py scripts/verify_sources.py          # 有问题返回 1，全过返回 0
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'data' / 'index' / 'docs.jsonl'
PERSONAS = [ROOT / 'personas' / 'yuki.json', ROOT / 'personas' / 'yuki_school.json']

TAG = re.compile(r'^verified:(?P<ep>[^@]+)@(?P<time>\d+:\d+(?::\d+)?)$')


def norm(s):
    """比对用的归一化：只去掉空白，以及末尾那个为了成句补上的句号。

    为什么不再「去掉所有标点」：第一版是 `re.sub(r'[\\s\\W_]+', '', s)`，
    它把感叹号、问号、逗号、emoji 全剥掉了，于是

        「不需要!!!」「不需要?!」「，“不需要，”」「不需要😀」

    全部判为与原文「不需要」**一致**。而**感叹号恰恰是这个项目最硬的一条禁令**
    （长门的台词里一个感叹号都没有，`banned_patterns` 专门禁它）——
    一个能把 `不需要!!!` 说成「与原文逐字一致」的核对脚本，比没有还坏：
    它会给出一个假的“已核对”结论。

    现在的规则：空白不影响比对（原文里台词之间用空格或全角空格分隔）；
    句末的一个「。」允许有（示例为了成句会补标点）；
    其余任何字符差异都算不一致，并如实报出来。
    """
    s = re.sub(r'\s+', '', str(s))
    return s[:-1] if s.endswith('。') else s


def load_index():
    if not INDEX.exists():
        return None
    rows = []
    with INDEX.open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    rows = load_index()
    if rows is None:
        print(f'跳过：找不到语料索引 {INDEX.relative_to(ROOT)}')
        print('（语料不入库，克隆后需要自备 data/parsed 并跑 py build_corpus.py）')
        return 0

    # (集, 起始时间码) -> [原文, ...]
    by_slot = {}
    for r in rows:
        by_slot.setdefault((r['ep'], r['start']), []).append(r['text'])

    # 人格卡**缺一张就报错**。第一版写的是 `if not path.exists(): continue`，
    # 于是把 yuki.json 改名后，脚本会打印「核对 0 条 → 全部一致」并返回 0。
    # 核对 0 条不是「全对」，是「没核对」——这两件事必须分开。
    missing = [p.name for p in PERSONAS if not p.exists()]
    if missing:
        print(f'找不到人格卡：{"、".join(missing)}')
        print('核对 0 条不等于通过。请检查文件是否被改名或移动。')
        return 1

    checked = 0
    bad = []
    ambiguous = []
    for path in PERSONAS:
        card = json.loads(path.read_text(encoding='utf-8'))
        for i, e in enumerate(card.get('examples') or []):
            src = str(e.get('source') or '')
            m = TAG.match(src)
            if not m:
                continue
            checked += 1
            ep, time = m.group('ep'), m.group('time')
            texts = by_slot.get((ep, time))
            where = f'{path.name} 第 {i + 1} 条 {src}'
            if not texts:
                bad.append(f'{where}：索引里没有 第{ep}集 {time} 这一句')
                continue
            want = norm(e.get('assistant', ''))
            if not any(norm(t) == want for t in texts):
                got = ' / '.join(repr(t[:40]) for t in texts)
                bad.append(
                    f'{where}：标签说这一句是原文，但原文是 {got}，'
                    f'示例写的是 {e.get("assistant")!r}')
            elif len(texts) > 1:
                # 同一（集, 时间码）在索引里可能有多条（226 处）。任一条对上就算过，
                # 这在「只验证这句话确实出现在这个时间点」的意义上是对的；
                # 但要**明确说出来**，否则会让人以为时间码能唯一确定一句话。
                ambiguous.append(f'{where}：该时间码有 {len(texts)} 条台词，'
                                 f'示例与其中一条一致')

    print(f'核对 verified 标签 {checked} 条')
    for msg in ambiguous:
        print(f'  · {msg}')
    if bad:
        for msg in bad:
            print(f'  × {msg}')
        print(f'\n{len(bad)} 条对不上。要么改示例去贴合原文，要么把标签降级为 '
              f'reading:（表示这是人物解读，不是原句）。')
        return 1
    if checked == 0:
        # 一条都没核对就返回 0，与「全部通过」在退出码上无法区分。
        # 现在没有 verified 示例是正常的（只有 1 条），但将来若被误删，
        # 这里必须说出来，而不是安静地给个 0。
        print('  ! 没有任何 verified 标签可核对。这不是通过，是没核对。')
        print('    如果这是有意的（示例已全部改为 reading:），可以把这条提示忽略；')
        print('    如果是意外删掉了标签，源核对就形同虚设。')
        return 0
    print('  → 全部与索引逐字一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
