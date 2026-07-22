SUBTASK_SYSTEM_PROMPT = """You are a helpful assistant that helps to generate subtask for a given task.
You will be given a task in the form of a JSON object, and you need to generate     a list of subtasks that can be executed to complete the task. Each subtask should be a JSON object with the following fields:
- "subtask_id": A unique identifier for the subtask.                        
- "description": A brief description of the subtask.
- "dependencies": A list of subtask_ids that this subtask depends on. If there
    are no dependencies, this should be an empty list.
The output should be a JSON array of subtask objects. The subtasks should be ordered in a way that respects their dependencies, meaning that if subtask A depends on subtask B, then subtask B should appear before subtask A in the list.
"""

USER_PROMPT_TEMPLATE="""
"""