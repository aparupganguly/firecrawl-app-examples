import streamlit as st
import asyncio
from typing import AsyncGenerator, Generator
import threading
import queue

from src.config import DEFAULT_MAX_URLS, DEFAULT_USE_FULL_TEXT
from src.llms_text import extract_website_content
from src.agents import extract_domain_knowledge, create_domain_agent
from agents import Runner
from openai.types.responses import ResponseTextDeltaEvent

# Initialize session state
def init_session_state():
    if 'domain_agent' not in st.session_state:
        st.session_state.domain_agent = None
    if 'domain_knowledge' not in st.session_state:
        st.session_state.domain_knowledge = None
    if 'messages' not in st.session_state:
        st.session_state.messages = []
    if 'extraction_status' not in st.session_state:
        st.session_state.extraction_status = None

def run_app():
    # Initialize session state
    init_session_state()
    
    # App title and description in main content area
    st.title("WebToAgent")
    st.subheader("Extract domain knowledge from any website and create specialized AI agents.")
    
    # Display welcome message using AI chat message component
    if not st.session_state.domain_agent:
        with st.chat_message("assistant"):
            st.markdown("👋 Welcome! Enter a website URL in the sidebar, and I'll transform it into an AI agent you can chat with.")
    
    # Form elements in sidebar
    st.sidebar.title("Create your agent")
    
    website_url = st.sidebar.text_input("Enter website URL", placeholder="https://example.com")
    
    max_pages = st.sidebar.slider("Maximum pages to analyze", 1, 25, DEFAULT_MAX_URLS, 
                         help="More pages means more comprehensive knowledge but longer processing time. Capped at 25 pages to respect rate limits.")
    
    use_full_text = st.sidebar.checkbox("Use comprehensive text extraction", value=DEFAULT_USE_FULL_TEXT,
                                help="Extract full contents of each page (may increase processing time)")
    
    submit_button = st.sidebar.button("Create agent", type="primary")
    
    # Process form submission
    if submit_button and website_url:
        st.session_state.extraction_status = "extracting"
        
        try:
            with st.spinner("Extracting website content with Firecrawl..."):
                content = extract_website_content(
                    url=website_url, 
                    max_urls=max_pages,
                    show_full_text=use_full_text
                )
                
                # Show content sample
                with st.expander("View extracted content sample"):
                    st.text(content['llmstxt'][:1000] + "...")
                
                # Process content to extract knowledge
                with st.spinner("Analyzing content and generating knowledge model..."):
                    domain_knowledge = asyncio.run(extract_domain_knowledge(
                        content['llmstxt'] if not use_full_text else content['llmsfulltxt'],
                        website_url
                    ))
                    
                    # Store in session state
                    st.session_state.domain_knowledge = domain_knowledge
                
                # Create specialized agent
                with st.spinner("Creating specialized agent..."):
                    domain_agent = create_domain_agent(domain_knowledge)
                    
                    # Store in session state
                    st.session_state.domain_agent = domain_agent
                    
                    st.session_state.extraction_status = "complete"
                    st.success("Agent created successfully! You can now chat with the agent.")
        
        except Exception as e:
            st.error(f"Error: {str(e)}")
            st.session_state.extraction_status = "failed"
    
    # Chat interface
    if st.session_state.domain_agent:
        display_chat_interface()

def stream_agent_response(agent, prompt):
    """Stream agent response using a background thread and a queue for real-time token streaming."""
    # Create a queue to transfer tokens from async thread to main thread
    token_queue = queue.Queue()
    
    # Flag to signal when the async function is complete
    done_event = threading.Event()
    
    # To collect the full response for the chat history
    full_response = []
    
    # The thread function to run the async event loop
    def run_async_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def process_stream():
            try:
                result = Runner.run_streamed(agent, prompt)
                
                # Process all stream events
                async for event in result.stream_events():
                    # Only handle text delta events
                    if (event.type == "raw_response_event" and 
                        isinstance(event.data, ResponseTextDeltaEvent) and 
                        event.data.delta):
                        # Put the token in the queue
                        token_queue.put(event.data.delta)
                        full_response.append(event.data.delta)
                
                # If no tokens were yielded, use the final output
                if not full_response and hasattr(result, 'final_output') and result.final_output:
                    token_queue.put(result.final_output)
                    full_response.append(result.final_output)
            except Exception as e:
                # Put the exception in the queue to be raised in the main thread
                token_queue.put(e)
            finally:
                # Signal that we're done processing
                done_event.set()
                # Always put a None to indicate end of stream
                token_queue.put(None)
        
        try:
            loop.run_until_complete(process_stream())
        finally:
            loop.close()
    
    # Start the background thread
    thread = threading.Thread(target=run_async_loop)
    thread.daemon = True
    thread.start()
    
    # Generator function to yield tokens from the queue
    def token_generator():
        while not done_event.is_set() or not token_queue.empty():
            try:
                token = token_queue.get(timeout=0.1)
                if token is None:
                    # End of stream
                    break
                elif isinstance(token, Exception):
                    # Re-raise exceptions from the background thread
                    raise token
                else:
                    yield token
            except queue.Empty:
                # Queue timeout, just continue waiting
                continue
    
    return token_generator(), ''.join(full_response)

def get_non_streaming_response(agent, prompt):
    """Fallback function for non-streaming response."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(Runner.run(agent, prompt))
        return result.final_output
    finally:
        loop.close()

def display_chat_interface():
    """Display chat interface for interacting with the domain agent."""
    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    
    # Chat input
    if prompt := st.chat_input("Ask a question about this domain..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        # Display user message
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # Get agent response with streaming
        with st.chat_message("assistant"):
            try:
                # Create the generator for streaming and the full response collection
                token_generator, full_response_future = stream_agent_response(st.session_state.domain_agent, prompt)
                
                # Stream the response tokens
                st.write_stream(token_generator)
                
                # Only add to the history if there was actual content
                if full_response_future:
                    # Add assistant response to chat history
                    st.session_state.messages.append({"role": "assistant", "content": full_response_future})
            except Exception as e:
                # Fallback to non-streaming response if streaming fails
                st.warning(f"Streaming failed ({str(e)}), using standard response method.")
                try:
                    full_response = get_non_streaming_response(st.session_state.domain_agent, prompt)
                    st.markdown(full_response)
                    st.session_state.messages.append({"role": "assistant", "content": full_response})
                except Exception as e2:
                    st.error(f"Error generating response: {str(e2)}")

if __name__ == "__main__":
    run_app()
