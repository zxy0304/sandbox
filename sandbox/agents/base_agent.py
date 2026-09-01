"""Agent 基类。

这个文件提供所有角色代理共用的最小接口：保存代理名称/配置，
并在每次生成前统一检查上下文字段，避免每个具体代理重复写样板校验。
"""

class BaseAgent:
    """统一代理对象的基础行为。

    具体代理只需要实现 ``generate(context)``；公共的名称、配置和
    context 必填字段检查都放在这里，runner 可以因此用同一种方式调用角色。
    """
    def __init__(self, name=None, config=None):
        """初始化代理名称和配置。

        如果调用方没有显式传 name，就用类名作为默认名称；config 使用空 dict
        兜底，这样后续读取配置时不需要反复判断 None。
        """
        self.name = name or self.__class__.__name__
        self.config = config or {}

    def generate(self, context):
        """代理主入口，占位要求子类实现。

        runner 只依赖这个统一方法；基类主动抛出 NotImplementedError，
        可以让遗漏实现的子类在开发期尽快暴露问题。
        """
        raise NotImplementedError("%s must implement generate(context)" % self.name)

    def validate_context(self, context, required_keys=None):
        """检查上下文类型和必填字段。

        通过传入 required_keys，具体代理可以声明自己需要哪些输入；
        缺字段时集中抛出带代理名称的错误，便于定位是哪个角色的输入拼装错了。
        """
        if not isinstance(context, dict):
            raise ValueError("%s context must be a dict" % self.name)
        required_keys = required_keys or []
        missing = []
        for key in required_keys:
            if key not in context:
                missing.append(key)
        if missing:
            raise ValueError("%s missing required context keys: %s" % (self.name, ", ".join(missing)))
