from __future__ import annotations


def semantic_tag_defs() -> dict[str, dict[str, object]]:
    return {
        "csharp_app": {
            "module_hints": ["DTOs", "Entities", "Enums", "Interfaces", "Services", "ViewComponents", "Authorization", "Hubs", "Reports", "Properties"],
            "role_patterns": [
                ("**/DTOs/**/*.cs", "data-model"),
                ("**/Entities/**/*.cs", "data-model"),
                ("**/Enums/**/*.cs", "data-model"),
                ("**/Interfaces/**/*.cs", "abstraction"),
                ("**/*Controller.cs", "controller"),
                ("**/Services/**/*.cs", "service"),
                ("**/*Service.cs", "service"),
                ("**/*ViewComponent.cs", "component"),
                ("**/ViewComponents/**/*.cs", "component"),
                ("**/*Hub.cs", "hub"),
                ("**/Hubs/**/*.cs", "hub"),
                ("**/Authorization/**/*.cs", "policy"),
                ("**/*Handler.cs", "policy"),
                ("**/*Requirement.cs", "policy"),
                ("**/*Policy.cs", "policy"),
                ("**/Reports/**/*.cs", "report"),
                ("**/Properties/**/*.cs", "configuration"),
            ],
        },
        "javascript_app": {
            "module_hints": ["components", "features", "hooks", "services", "workers", "middleware", "api", "server", "runtime", "scripts"],
            "role_patterns": [
                ("**/hooks/**/*.js", "hook-module"),
                ("**/components/**/index.js", "barrel-module"),
                ("**/*Provider.jsx", "context-provider"),
                ("**/components/**/*.js", "component"),
                ("**/features/**/index.js", "barrel-module"),
                ("**/services/**/*.js", "service"),
                ("**/api/**/*.js", "route-handler"),
                ("**/server/**/*.js", "server-module"),
                ("**/runtime/**/*.js", "server-module"),
                ("**/middleware*.js", "middleware"),
                ("**/workers/**/*.js", "worker"),
                ("**/scripts/**/*.js", "script"),
            ],
        },
        "typescript_app": {
            "module_hints": ["components", "features", "hooks", "services", "workers", "middleware", "api", "server", "runtime", "scripts", "models", "types"],
            "role_patterns": [
                ("**/hooks/**/*.ts", "hook-module"),
                ("**/components/**/index.ts", "barrel-module"),
                ("**/components/**/*.ts", "component"),
                ("**/features/**/index.ts", "barrel-module"),
                ("**/services/**/*.ts", "service"),
                ("**/api/**/*.ts", "route-handler"),
                ("**/server/**/*.ts", "server-module"),
                ("**/runtime/**/*.ts", "server-module"),
                ("**/middleware*.ts", "middleware"),
                ("**/workers/**/*.ts", "worker"),
                ("**/scripts/**/*.ts", "script"),
                ("**/models/**/*.ts", "data-model"),
                ("**/types/**/*.ts", "data-model"),
            ],
        },
        "jsx_app": {
            "module_hints": ["pages", "components", "hooks"],
            "role_patterns": [
                ("**/pages/**/*.jsx", "page"),
                ("**/*Provider.jsx", "context-provider"),
                ("**/components/**/*.jsx", "component"),
                ("**/hooks/**/*.jsx", "hook-module"),
            ],
        },
        "tsx_app": {
            "module_hints": ["pages", "components", "hooks", "layouts"],
            "role_patterns": [
                ("**/pages/**/*.tsx", "page"),
                ("**/*Provider.tsx", "context-provider"),
                ("**/components/**/index.tsx", "barrel-module"),
                ("**/components/**/*.tsx", "component"),
                ("**/hooks/**/*.tsx", "hook-module"),
                ("**/layouts/**/*.tsx", "layout"),
            ],
        },
        "python_app": {
            "module_hints": ["scripts", "bin", "cli", "tools", "utils", "helpers"],
            "role_patterns": [
                ("**/__init__.py", "module-init"),
                ("**/scripts/**/*.py", "script"),
                ("**/bin/**/*.py", "script"),
                ("**/cli/**/*.py", "script"),
                ("**/tools/**/*.py", "script"),
                ("**/utils/**/*.py", "utility-module"),
                ("**/helpers/**/*.py", "utility-module"),
            ],
        },
        "razor_views": {
            "module_hints": ["Pages", "Shared"],
            "role_patterns": [
                ("**/_Layout.cshtml", "layout"),
                ("**/_ViewStart.cshtml", "configuration"),
                ("**/_ViewImports.cshtml", "configuration"),
                ("**/Shared/**/*.cshtml", "shared-view"),
            ],
        },
    }
