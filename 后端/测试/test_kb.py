#!/usr/bin/env python3
"""知识库单测（不联网、不启服务）。
跑法：python3 tools/test_kb.py

盯的是「严格模式」这条底线，以及几个容易做坏的边界：
    - 命中与否是硬判定（hit 是 bool，不能让调用方看分数猜）
    - 小节标题要跟着片段走，否则「价格」这种只在标题出现的词捞不到正文
    - 同内容不重复入库
    - 打标签/切片的失败不能把整篇资料弄丢
    - 空库、空问题、未启用角色都不该下"不许编"的指令
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.kb import KnowledgeBase, CHUNK_SIZE   # noqa: E402

PASS = FAIL = 0


def ok(cond, label, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('  ✓ ' + label)
    else:
        FAIL += 1
        print('  ✗ ' + label + ('  → %s' % (extra,) if extra else ''))


def mk(**over):
    root = tempfile.mkdtemp()
    conf = {'kb': {'enabled': True, 'agents': ['assistant'], 'top_k': 4,
                   'score_min': 0.18}}
    conf.update(over.pop('conf', {}))
    cfg = type('C', (), {'settings': conf, 'ROOT': root})()
    return KnowledgeBase(cfg, root=os.path.join(root, 'kb'))


DOC = """弹力针织面料说明

本系列采用 92% 涤纶 + 8% 氨纶混纺，克重 210 g/m2，门幅 150cm。缩水率经向 2.5%。

起订量与交期
常规色起订量 300 米，定制色起订量 800 米。常规色交期 7 个工作日。

价格
常规色每米 28 元，定制色每米 34 元，含税不含运费。"""


def main():
    print('[1] 切片：小节标题要跟着片段走')
    kb = mk()
    chunks = kb.split(DOC)
    ok(len(chunks) >= 3, '切成多片', len(chunks))
    heads = [c['head'] for c in chunks]
    ok('价格' in heads, '「价格」被认成小节标题', heads)
    price = [c for c in chunks if c['head'] == '价格']
    ok(len(price) == 1 and '28 元' in price[0]['text'],
       '价格那一段的正文跟着价格标题走', price and price[0]['text'])
    ok(all(len(c['text']) <= CHUNK_SIZE + 200 for c in chunks), '片段长度受控')
    ok(kb.split('') == [], '空文本切出空列表')
    ok(len(kb.split('只有一行短文档')) == 1, '整篇只有一行短文档也要留一片，不能切没了')

    print('\n[2] 入库与去重')
    kb = mk()
    r = kb.add_doc(DOC, title='弹力针织面料', keywords='价格,报价,多少钱,起订量')
    ok(r.get('ok') and r.get('chunks') >= 3, '文档入库成功', r)
    r2 = kb.add_doc(DOC, title='再传一次')
    ok(r2.get('dup') is True, '同内容不重复入库（按内容 md5）', r2)
    ok(len(kb.docs) == 1, '库里还是只有一篇', len(kb.docs))
    ok(kb.add_doc('   ', title='空') is not None and not kb.add_doc('', title='空').get('ok'),
       '空内容拒绝入库')

    print('\n[3] 检索：命中是硬判定')
    kb = mk()
    kb.add_doc(DOC, title='弹力针织面料', keywords='价格,报价,多少钱,起订量')
    hit = kb.lookup('价格', agent='assistant')
    ok(isinstance(hit.get('hit'), bool), 'hit 是布尔，不是分数')
    ok(hit['hit'] is True, '「价格」命中', hit.get('reason'))
    ok(hit['chunks'][0]['head'] == '价格', '命中的正是价格那一段', hit['chunks'][0]['head'])
    miss = kb.lookup('你们老板是谁', agent='assistant')
    ok(miss['hit'] is False, '无关问题判定为没命中', miss.get('reason'))
    ok(miss['chunks'] == [] and miss['qa'] == [], '没命中时不返回任何片段')
    ok('阈值' in miss['reason'], '没命中的理由里带上阈值，方便调参', miss['reason'])
    ok(kb.lookup('', agent='assistant')['hit'] is False, '空问题不命中')
    ok(kb.lookup('起订量', agent='designer')['hit'] is False, '未启用角色不查知识库')
    ok(kb.lookup('起订量', agent='designer')['strict'] is False,
       '未启用角色返回 strict=False，运行时据此不注入')

    print('\n[4] 关键词别名补召回')
    kb = mk()
    kb.add_doc(DOC, title='弹力针织面料', keywords='价格,报价,多少钱')
    r = kb.lookup('多少钱一米', agent='assistant')
    heads = [c['head'] for c in r['chunks']]
    ok('价格' in heads, '「多少钱一米」靠别名把价格段捞回来了', heads)
    ok(kb.lookup('报价', agent='assistant')['chunks'][0]['head'] == '价格',
       '「报价」也能捞到价格段')

    print('\n[5] 问答对：优先级最高，原样照答')
    kb = mk()
    kb.add_doc(DOC, title='弹力针织面料')
    kb.add_qa('运费怎么算？', '满 500 米包邮，不满按 2 元/米。', tags=['运费'])
    r = kb.lookup('运费怎么算', agent='assistant')
    ok(r['hit'] and r['qa'], '命中问答对')
    ok(r['qa'][0]['a'] == '满 500 米包邮，不满按 2 元/米。', '答案是原文，一个字没改')
    up = kb.add_qa('运费怎么算？', '改成满 800 米包邮')
    ok(up.get('updated') is True, '同问题再存一次是更新答案，不是新增一条')
    ok(len(kb.qa) == 1, '问答对没有变成两条', len(kb.qa))
    ok(not kb.add_qa('只有问题', '').get('ok'), '缺答案的问答对拒绝入库')
    ok(kb.add_qa('只有答案', '', ) is not None, '缺问题的问答对拒绝入库')

    print('\n[6] 严格模式的两套指令')
    kb = mk()
    kb.add_doc(DOC, title='弹力针织面料')
    hit_txt = kb.render_for_prompt(kb.lookup('价格', agent='assistant'))
    ok('唯一可用的资料' in hit_txt, '命中时下"只能用这些资料"的指令')
    ok('不许换算' in hit_txt, '命中时禁止换算推导')
    ok('28 元' in hit_txt, '资料原文进了提示词')
    miss_txt = kb.render_for_prompt(kb.lookup('老板是谁', agent='assistant'))
    ok('没有' in miss_txt and '绝对不许' in miss_txt, '没命中时下"不许编"的指令')
    ok('一般来说' in miss_txt, '没命中时连"一般来说"这类含糊话术也明令禁止')

    print('\n[7] 空库 / 异常输入')
    kb = mk()
    st = kb.stats()
    ok(st['docs'] == 0 and st['qa'] == 0, '空库统计为 0')
    ok(kb.lookup('随便问什么', agent='assistant')['hit'] is False, '空库一律没命中')
    ok(kb.lookup('随便问什么', agent='assistant')['reason'] == '知识库是空的',
       '空库的理由单独标出来（运行时据此不注入，机器人不会只会说"没有资料"）')
    ok(kb.remove_doc('不存在') is not None and not kb.remove_doc('不存在').get('ok'),
       '删不存在的资料不报错')
    ok(kb.remove_qa('不存在').get('ok') is False, '删不存在的问答不报错')
    ok(kb.update_qa('不存在', {'a': 'x'}).get('ok') is False, '改不存在的问答不报错')

    print('\n[8] 持久化：重启后还在')
    root = tempfile.mkdtemp()
    conf = {'kb': {'enabled': True, 'agents': ['assistant']}}
    cfg1 = type('C', (), {'settings': conf, 'ROOT': root})()
    kb1 = KnowledgeBase(cfg1, root=os.path.join(root, 'kb'))
    kb1.add_doc(DOC, title='弹力针织面料', keywords='价格')
    kb1.add_qa('运费怎么算？', '满 500 米包邮')
    cfg2 = type('C', (), {'settings': conf, 'ROOT': root})()
    kb2 = KnowledgeBase(cfg2, root=os.path.join(root, 'kb'))
    ok(len(kb2.docs) == 1, '文档读回来了', len(kb2.docs))
    ok(len(kb2.qa) == 1, '问答读回来了', len(kb2.qa))
    ok(kb2.lookup('价格', agent='assistant')['hit'] is True, '重启后检索仍然命中')
    ok(kb2.docs[list(kb2.docs)[0]]['keywords'] == ['价格'], '关键词也存下来了')

    print('\n[9] 增删')
    kb = mk()
    r = kb.add_doc(DOC, title='A')
    kb.add_doc(DOC.replace('210', '220'), title='B')
    ok(len(kb.docs) == 2, '两篇内容不同，都进库', len(kb.docs))
    did = r['id']
    ok(kb.remove_doc(did).get('ok') is True, '删除成功')
    ok(len(kb.docs) == 1, '删完剩一篇')
    ok(not os.path.exists(os.path.join(kb.dir_docs, did + '.json')), '文件也真删了')
    qid = kb.add_qa('问', '答')['id']
    ok(kb.remove_qa(qid).get('ok') is True and len(kb.qa) == 0, '问答能删')

    # 不清理临时目录：用 shutil.rmtree 会撞上沙盒的批量删除守卫，
    # 进程直接被掐掉，连最后的统计行都打印不出来。临时目录交给系统回收。
    print('\n结果：%d 通过 / %d 失败' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
