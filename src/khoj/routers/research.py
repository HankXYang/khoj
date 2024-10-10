import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from fastapi import Request

from khoj.database.adapters import ConversationAdapters, EntryAdapters
from khoj.database.models import Agent, KhojUser
from khoj.processor.conversation import prompts
from khoj.processor.conversation.utils import (
    InformationCollectionIteration,
    construct_iteration_history,
    remove_json_codeblock,
)
from khoj.processor.tools.online_search import read_webpages, search_online
from khoj.processor.tools.run_code import run_code
from khoj.routers.api import extract_references_and_questions
from khoj.routers.helpers import (
    ChatEvent,
    construct_chat_history,
    extract_relevant_info,
    generate_summary_from_files,
    send_message_to_model_wrapper,
)
from khoj.utils.helpers import (
    ConversationCommand,
    function_calling_description_for_llm,
    is_none_or_empty,
    timer,
)
from khoj.utils.rawconfig import LocationData

logger = logging.getLogger(__name__)


async def apick_next_tool(
    query: str,
    conversation_history: dict,
    subscribed: bool,
    uploaded_image_url: str = None,
    location: LocationData = None,
    user_name: str = None,
    agent: Agent = None,
    previous_iterations_history: str = None,
    max_iterations: int = 5,
):
    """
    Given a query, determine which of the available tools the agent should use in order to answer appropriately. One at a time, and it's able to use subsequent iterations to refine the answer.
    """

    tool_options = dict()
    tool_options_str = ""

    agent_tools = agent.input_tools if agent else []

    for tool, description in function_calling_description_for_llm.items():
        tool_options[tool.value] = description
        if len(agent_tools) == 0 or tool.value in agent_tools:
            tool_options_str += f'- "{tool.value}": "{description}"\n'

    chat_history = construct_chat_history(conversation_history)

    if uploaded_image_url:
        query = f"[placeholder for user attached image]\n{query}"

    personality_context = (
        prompts.personality_context.format(personality=agent.personality) if agent and agent.personality else ""
    )

    # Extract Past User Message and Inferred Questions from Conversation Log
    today = datetime.today()
    location_data = f"{location}" if location else "Unknown"
    username = prompts.user_name.format(name=user_name) if user_name else ""

    function_planning_prompt = prompts.plan_function_execution.format(
        query=query,
        tools=tool_options_str,
        chat_history=chat_history,
        personality_context=personality_context,
        current_date=today.strftime("%Y-%m-%d"),
        day_of_week=today.strftime("%A"),
        username=username,
        location=location_data,
        previous_iterations=previous_iterations_history,
        max_iterations=max_iterations,
    )

    chat_model_option = await ConversationAdapters.aget_advanced_conversation_config()

    with timer("Chat actor: Infer information sources to refer", logger):
        response = await send_message_to_model_wrapper(
            function_planning_prompt,
            response_type="json_object",
            subscribed=subscribed,
            chat_model_option=chat_model_option,
        )

    try:
        response = response.strip()
        response = remove_json_codeblock(response)
        response = json.loads(response)
        suggested_data_source = response.get("data_source", None)
        suggested_query = response.get("query", None)

        logger.info(f"Response for determining relevant tools: {response}")

        return InformationCollectionIteration(
            data_source=suggested_data_source,
            query=suggested_query,
        )

    except Exception as e:
        logger.error(f"Invalid response for determining relevant tools: {response}. {e}", exc_info=True)
        return InformationCollectionIteration(
            data_source=None,
            query=None,
        )


async def execute_information_collection(
    request: Request,
    user: KhojUser,
    query: str,
    conversation_id: str,
    conversation_history: dict,
    subscribed: bool,
    uploaded_image_url: str = None,
    agent: Agent = None,
    send_status_func: Optional[Callable] = None,
    user_name: str = None,
    location: LocationData = None,
    file_filters: List[str] = [],
):
    current_iteration = 0
    MAX_ITERATIONS = 2
    previous_iterations: List[InformationCollectionIteration] = []
    while current_iteration < MAX_ITERATIONS:
        online_results: Dict = dict()
        code_results: Dict = dict()
        compiled_references: List[Any] = []
        inferred_queries: List[Any] = []
        previous_iterations_history = construct_iteration_history(previous_iterations, prompts.previous_iteration)

        result: str = ""

        this_iteration = await apick_next_tool(
            query,
            conversation_history,
            subscribed,
            uploaded_image_url,
            location,
            user_name,
            agent,
            previous_iterations_history,
            MAX_ITERATIONS,
        )
        if this_iteration.data_source == ConversationCommand.Notes:
            ## Extract Document References
            compiled_references, inferred_queries, defiltered_query = [], [], None
            async for result in extract_references_and_questions(
                request,
                conversation_history,
                this_iteration.query,
                7,
                None,
                conversation_id,
                [ConversationCommand.Default],
                location,
                send_status_func,
                uploaded_image_url=uploaded_image_url,
                agent=agent,
            ):
                if isinstance(result, dict) and ChatEvent.STATUS in result:
                    yield result[ChatEvent.STATUS]
                else:
                    compiled_references.extend(result[0])
                    inferred_queries.extend(result[1])
                    defiltered_query = result[2]
                    this_iteration.context = compiled_references

        if not is_none_or_empty(compiled_references):
            try:
                headings = "\n- " + "\n- ".join(set([c.get("compiled", c).split("\n")[0] for c in compiled_references]))
                # Strip only leading # from headings
                headings = headings.replace("#", "")
                async for result in send_status_func(f"**Found Relevant Notes**: {headings}"):
                    yield result
            except Exception as e:
                # TODO Get correct type for compiled across research notes extraction
                logger.error(f"Error extracting references: {e}", exc_info=True)

        elif this_iteration.data_source == ConversationCommand.Online:
            async for result in search_online(
                this_iteration.query,
                conversation_history,
                location,
                user,
                subscribed,
                send_status_func,
                [],
                uploaded_image_url=uploaded_image_url,
                agent=agent,
            ):
                if isinstance(result, dict) and ChatEvent.STATUS in result:
                    yield result[ChatEvent.STATUS]
                else:
                    online_results: Dict[str, Dict] = result  # type: ignore
                    this_iteration.onlineContext = online_results

        elif this_iteration.data_source == ConversationCommand.Webpage:
            try:
                async for result in read_webpages(
                    this_iteration.query,
                    conversation_history,
                    location,
                    user,
                    subscribed,
                    send_status_func,
                    uploaded_image_url=uploaded_image_url,
                    agent=agent,
                ):
                    if isinstance(result, dict) and ChatEvent.STATUS in result:
                        yield result[ChatEvent.STATUS]
                    else:
                        direct_web_pages: Dict[str, Dict] = result  # type: ignore

                        webpages = []
                        for web_query in direct_web_pages:
                            if online_results.get(web_query):
                                online_results[web_query]["webpages"] = direct_web_pages[web_query]["webpages"]
                            else:
                                online_results[web_query] = {"webpages": direct_web_pages[web_query]["webpages"]}

                            for webpage in direct_web_pages[web_query]["webpages"]:
                                webpages.append(webpage["link"])
                        this_iteration.onlineContext = online_results
            except Exception as e:
                logger.error(f"Error reading webpages: {e}", exc_info=True)

        elif this_iteration.data_source == ConversationCommand.Code:
            try:
                async for result in run_code(
                    this_iteration.query,
                    conversation_history,
                    previous_iterations_history,
                    location,
                    user,
                    send_status_func,
                    uploaded_image_url=uploaded_image_url,
                    agent=agent,
                ):
                    if isinstance(result, dict) and ChatEvent.STATUS in result:
                        yield result[ChatEvent.STATUS]
                    else:
                        code_results: Dict[str, Dict] = result  # type: ignore
                        this_iteration.codeContext = code_results
                async for result in send_status_func(f"**Ran code snippets**: {len(this_iteration.codeContext)}"):
                    yield result
            except ValueError as e:
                logger.warning(
                    f"Failed to use code tool: {e}. Attempting to respond without code results",
                    exc_info=True,
                )

        # TODO: Fix summarize later
        # elif this_iteration.data_source == ConversationCommand.Summarize:
        #     response_log = ""
        #     agent_has_entries = await EntryAdapters.aagent_has_entries(agent)
        #     if len(file_filters) == 0 and not agent_has_entries:
        #         previous_iterations.append(
        #             InformationCollectionIteration(
        #                 data_source=this_iteration.data_source,
        #                 query=this_iteration.query,
        #                 context="No files selected for summarization.",
        #             )
        #         )
        #     elif len(file_filters) > 1 and not agent_has_entries:
        #         response_log = "Only one file can be selected for summarization."
        #         previous_iterations.append(
        #             InformationCollectionIteration(
        #                 data_source=this_iteration.data_source,
        #                 query=this_iteration.query,
        #                 context=response_log,
        #             )
        #         )
        # else:
        #     async for response in generate_summary_from_files(
        #         q=query,
        #         user=user,
        #         file_filters=file_filters,
        #         meta_log=conversation_history,
        #         subscribed=subscribed,
        #         send_status_func=send_status_func,
        #     ):
        #         if isinstance(response, dict) and ChatEvent.STATUS in response:
        #             yield response[ChatEvent.STATUS]
        #         else:
        #             response_log = response  # type: ignore
        #             previous_iterations.append(
        #                 InformationCollectionIteration(
        #                     data_source=this_iteration.data_source,
        #                     query=this_iteration.query,
        #                     context=response_log,
        #                 )
        #             )
        else:
            current_iteration = MAX_ITERATIONS

        current_iteration += 1

        if compiled_references or online_results or code_results:
            results_data = f"**Results**:\n"
            if compiled_references:
                results_data += f"**Document References**: {compiled_references}\n"
            if online_results:
                results_data += f"**Online Results**: {online_results}\n"
            if code_results:
                results_data += f"**Code Results**: {code_results}\n"

            # intermediate_result = await extract_relevant_info(this_iteration.query, results_data, agent)
            this_iteration.summarizedResult = results_data

        previous_iterations.append(this_iteration)
        yield this_iteration
